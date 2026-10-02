#!/usr/bin/env python3
"""trigger.py -- the org-event.yml verify job.

Two entry points, matched to the two-phase mint in the contract (§7 "Validate inputs BEFORE any
mint"):

- `validate`: format-checks the 5 relay inputs (digits, the owner/name regex, the GUID shape,
  `event == issue_comment`) and pins `repository`'s owner to $GITHUB_REPOSITORY_OWNER, all BEFORE
  any App token exists. On success it writes `valid=true` plus the bare repo `name` (for the mint
  step's `repositories:` input); on failure, `valid=false` plus a `reason`. Nothing else.
- `run`: takes the already-minted Board App token and the bot login (derived by the workflow from
  the mint step's own `app-slug` output, e.g. `<slug>[bot]` -- never a hand-maintained var) and
  runs the rest of the §3 checks in order through ONE injectable HTTP function (`http_request`),
  so tests never hit the network: the comment fetch (+ its issue_url match), PR-and-open, the §8
  command parse (`@review [MODEL] [EFFORT]`, resolved against `models.json` in this checkout),
  org membership, recency (15 minutes, through an injectable clock), the paged/bot-scoped
  idempotency marker (§7), and private-repo. An empty bot login is itself fatal
  (`config:bot-login`) -- it is never treated as "idempotency doesn't apply". On success it writes
  the contract's outputs to $GITHUB_OUTPUT (including the resolved `backend`/`model`/`effort`);
  on any failure -- including an unexpected one, e.g. a network error or a non-JSON response -- it
  writes `verified=false` plus a `reason` and exits 0 either way. It never echoes the comment
  body, anywhere.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from marker import ai_review_marker

API_ROOT = "https://api.github.com"
COMMAND_WORD = "@review"
MODELS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
RECENCY_LIMIT = timedelta(minutes=15)
HTTP_TIMEOUT_SECONDS = 10
COMMENTS_PAGE_SIZE = 100

NUMBER_RE = re.compile(r"^[0-9]+$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
RESERVED_PATH_SEGMENTS = (".", "..")

HttpFn = Callable[[str, str, str], tuple[int, Optional[dict]]]
ClockFn = Callable[[], datetime]


class VerifyFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def http_request(method: str, path: str, token: str) -> tuple[int, Optional[dict]]:
    """The one real HTTP call site. GET-only -- every §3 check reads, never writes. A network
    failure or a non-JSON body is reported as a status with no body, never a crash."""
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read()
    except urllib.error.URLError:
        return 0, None
    if not body:
        return status, None
    try:
        return status, json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, None


def validate_format(inputs: dict) -> None:
    if inputs.get("event") != "issue_comment":
        raise VerifyFailure("format:event")
    repository = inputs.get("repository", "")
    if not REPOSITORY_RE.fullmatch(repository):
        raise VerifyFailure("format:repository")
    owner, _, name = repository.partition("/")
    if owner in RESERVED_PATH_SEGMENTS or name in RESERVED_PATH_SEGMENTS:
        raise VerifyFailure("format:repository")
    if not NUMBER_RE.fullmatch(inputs.get("number", "")):
        raise VerifyFailure("format:number")
    if not NUMBER_RE.fullmatch(inputs.get("comment_id", "")):
        raise VerifyFailure("format:comment_id")
    if not GUID_RE.fullmatch(inputs.get("delivery_id", "")):
        raise VerifyFailure("format:delivery_id")


def validate_inputs(inputs: dict, repo_owner: str) -> str:
    """Format-checks all 5 inputs, then pins `repository`'s owner to `repo_owner` (§7 "Org
    pin"). Returns the bare repo name on success; raises VerifyFailure otherwise. Runs BEFORE
    any mint -- never fetches anything."""
    validate_format(inputs)
    owner, _, name = inputs["repository"].partition("/")
    if owner != repo_owner:
        raise VerifyFailure("format:owner_mismatch")
    return name


def fetch_comment(http: HttpFn, token: str, repository: str, comment_id: str, number: str) -> dict:
    status, body = http("GET", f"/repos/{repository}/issues/comments/{comment_id}", token)
    if status != 200 or body is None:
        raise VerifyFailure("comment:not_found")
    expected_issue_url = f"{API_ROOT}/repos/{repository}/issues/{number}"
    if body.get("issue_url") != expected_issue_url:
        raise VerifyFailure("comment:issue_mismatch")
    return body


def fetch_pull_request(http: HttpFn, token: str, repository: str, number: str) -> dict:
    status, body = http("GET", f"/repos/{repository}/pulls/{number}", token)
    if status != 200 or body is None:
        raise VerifyFailure("pr:not_found")
    if body.get("state") != "open":
        raise VerifyFailure("pr:not_open")
    return body


def load_models() -> dict:
    """§8: the allowlist, loaded from board-app's own checkout -- never from inputs."""
    with open(MODELS_PATH) as handle:
        return json.load(handle)


def parse_command(comment_body: str, models: dict) -> dict:
    """§8/§9 command grammar: the comment's FIRST line -- taken via `splitlines()[0]`, same as
    the relay, so e.g. a \\x0b vertical tab splits a line exactly where the relay's pre-filter
    would -- trimmed, split on whitespace: `@review [MODEL] [EFFORT]`, 1 to 3 tokens, token 1
    must equal `@review` exactly. MODEL is either an allowlist key or an exact listed `model`
    value; EFFORT must be in `efforts[backend]`. Omitted values take
    `default_model`/`default_effort`. An empty body is not a command."""
    lines = comment_body.splitlines()
    first_line = lines[0] if lines else ""
    tokens = first_line.strip().split()
    if not tokens or tokens[0] != COMMAND_WORD or len(tokens) > 3:
        raise VerifyFailure("command:no_match")

    model_token = tokens[1] if len(tokens) >= 2 else None
    effort_token = tokens[2] if len(tokens) >= 3 else None

    if model_token is None:
        entry = models["models"][models["default_model"]]
    elif model_token in models["models"]:
        entry = models["models"][model_token]
    else:
        entry = next((m for m in models["models"].values() if m["model"] == model_token), None)
        if entry is None:
            raise VerifyFailure("command:model")
    backend = entry["backend"]
    model = entry["model"]

    if effort_token is None:
        effort = models["default_effort"][backend]
    elif effort_token in models["efforts"][backend]:
        effort = effort_token
    else:
        raise VerifyFailure("command:effort")

    return {"command": "review", "backend": backend, "model": model, "effort": effort}


def check_membership(http: HttpFn, token: str, owner: str, login: str) -> None:
    status, _ = http("GET", f"/orgs/{owner}/members/{login}", token)
    if status != 204:
        raise VerifyFailure("membership:not_member")


def check_recency(created_at: str, clock: ClockFn) -> None:
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        raise VerifyFailure("recency:bad_timestamp")
    if clock() - created > RECENCY_LIMIT:
        raise VerifyFailure("recency:stale")


def check_idempotency(
    http: HttpFn,
    token: str,
    repository: str,
    number: str,
    comment_id: str,
    since: str,
    bot_login: str,
) -> None:
    """§7: the marker is matched ONLY on comments authored by the Board App bot, and the lookup
    pages through with since=<trigger comment created_at>&per_page=100 -- a marker comment buried
    past page 1 must still be found."""
    marker = ai_review_marker(comment_id)
    encoded_since = urllib.parse.quote(since, safe="")
    page = 1
    while True:
        status, body = http(
            "GET",
            f"/repos/{repository}/issues/{number}/comments"
            f"?since={encoded_since}&per_page={COMMENTS_PAGE_SIZE}&page={page}",
            token,
        )
        if status != 200 or body is None:
            raise VerifyFailure("idempotency:fetch_failed")
        for existing in body:
            if existing.get("user", {}).get("login") != bot_login:
                continue
            if marker in existing.get("body", ""):
                raise VerifyFailure("idempotency:duplicate")
        if len(body) < COMMENTS_PAGE_SIZE:
            return
        page += 1


def check_private(pull_request: dict) -> None:
    if not pull_request.get("base", {}).get("repo", {}).get("private", False):
        raise VerifyFailure("visibility:public")


def verify(
    inputs: dict,
    token: str,
    http: HttpFn,
    clock: ClockFn,
    repo_owner: str,
    bot_login: str,
) -> dict:
    validate_inputs(inputs, repo_owner)
    if not bot_login:
        # Never fall back to "" -- an empty bot login would make check_idempotency's
        # `login != bot_login` comparison match nothing, silently disabling idempotency.
        raise VerifyFailure("config:bot-login")

    repository = inputs["repository"]
    number = inputs["number"]
    comment_id = inputs["comment_id"]
    owner = repository.split("/", 1)[0]

    comment = fetch_comment(http, token, repository, comment_id, number)
    pull_request = fetch_pull_request(http, token, repository, number)
    parsed = parse_command(comment.get("body", ""), load_models())
    login = comment.get("user", {}).get("login", "")
    check_membership(http, token, owner, login)
    check_recency(comment.get("created_at", ""), clock)
    check_idempotency(http, token, repository, number, comment_id, comment.get("created_at", ""), bot_login)
    check_private(pull_request)

    return {
        "verified": "true",
        "repository": repository,
        "pr_number": number,
        "head_sha": pull_request.get("head", {}).get("sha", ""),
        "comment_id": comment_id,
        "command": parsed["command"],
        "requester": login,
        "backend": parsed["backend"],
        "model": parsed["model"],
        "effort": parsed["effort"],
    }


def write_outputs(github_output_path: str, outputs: dict) -> None:
    """Writes every key at once, after the full decision is made -- never a partial write."""
    with open(github_output_path, "a") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


def run_validate(inputs: dict, repo_owner: str, github_output_path: str) -> None:
    try:
        name = validate_inputs(inputs, repo_owner)
    except VerifyFailure as failure:
        write_outputs(github_output_path, {"valid": "false", "reason": failure.reason})
        return
    write_outputs(github_output_path, {"valid": "true", "name": name})


def run(
    inputs: dict,
    token: str,
    github_output_path: str,
    repo_owner: str,
    bot_login: str,
    http: HttpFn = http_request,
    clock: ClockFn = lambda: datetime.now(timezone.utc),
) -> None:
    try:
        outputs = verify(inputs, token, http, clock, repo_owner, bot_login)
    except VerifyFailure as failure:
        write_outputs(github_output_path, {"verified": "false", "reason": failure.reason})
        return
    except Exception:
        write_outputs(github_output_path, {"verified": "false", "reason": "error:unexpected"})
        return
    write_outputs(github_output_path, outputs)


def _inputs_from_env() -> dict:
    return {
        "event": os.environ.get("INPUT_EVENT", ""),
        "repository": os.environ.get("INPUT_REPOSITORY", ""),
        "number": os.environ.get("INPUT_NUMBER", ""),
        "comment_id": os.environ.get("INPUT_COMMENT_ID", ""),
        "delivery_id": os.environ.get("INPUT_DELIVERY_ID", ""),
    }


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    github_output_path = os.environ["GITHUB_OUTPUT"]
    repo_owner = os.environ["GITHUB_REPOSITORY_OWNER"]
    if mode == "validate":
        run_validate(_inputs_from_env(), repo_owner, github_output_path)
        sys.exit(0)
    if mode != "run":
        raise SystemExit(f"unknown mode: {mode!r} (expected 'validate' or 'run')")
    token = os.environ["BOARD_APP_TOKEN"]
    bot_login = os.environ.get("BOARD_APP_BOT_LOGIN", "")
    run(_inputs_from_env(), token, github_output_path, repo_owner, bot_login)
    sys.exit(0)


if __name__ == "__main__":
    main()
