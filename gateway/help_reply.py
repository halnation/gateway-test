#!/usr/bin/env python3
"""help_reply.py -- the §10 help reply: renders and upserts ONE comment on the PR when an
otherwise-legitimate `@review` request's command itself was malformed (command:grammar,
command:model, command:effort -- the `help` job only runs when trigger.py's `verify` set
`help=true`, which never happens for a non-member, a public repo, a stale/duplicate comment, or a
first token that wasn't even `@review`).

The reply is rendered entirely from `gateway/models.json` in this checkout plus the attempted
MODEL/EFFORT tokens trigger.py captured -- the raw comment body is never read here. Any
user-supplied token is HTML-escaped (`&`, `<`) and has its `@` neutralised with a zero-width
joiner before it ever appears in the rendered markdown, exactly like trigger.py never echoes the
comment body and like the house `neutralize()` convention in ai-comment.yml.

The comment carries the marker `<!-- board-app:ai-review-help comment:{comment_id} -->`, matched
ONLY on comments the Board App bot itself posted (bot login from $BOARD_APP_BOT_LOGIN, derived by
the workflow from the mint step's `app-slug`, same as ai_review_post.py) -- a repeat malformed
command on the same trigger comment updates that one reply instead of piling up new ones.

CLI: help_reply.py <reason> <attempted_model> <attempted_effort> <repository> <pr_number>
     <comment_id> <requester>
Env: GH_TOKEN (Board App token, pull_requests write), BOARD_APP_BOT_LOGIN, DRY_RUN (board.py's own
     convention: anything other than the literal string "0" is dry -- prints `{"would": ...}` to
     stderr instead of writing).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional

from trigger import load_models

API_ROOT = "https://api.github.com"
HELP_MARKER_TEMPLATE = "<!-- board-app:ai-review-help comment:{comment_id} -->"
HTTP_TIMEOUT_SECONDS = 10
COMMENTS_PAGE_SIZE = 100

HttpFn = Callable[[str, str, str, Optional[dict]], tuple[int, Optional[object]]]


def http_request(method: str, path: str, token: str, body: Optional[dict] = None) -> tuple[int, Optional[object]]:
    """The one real HTTP call site. Supports the GET (lookup) and POST/PATCH (upsert) this
    script needs; a network failure or a non-JSON body is reported as a status with no body."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = response.status
            response_body = response.read()
    except urllib.error.HTTPError as error:
        status = error.code
        response_body = error.read()
    except urllib.error.URLError:
        return 0, None
    if not response_body:
        return status, None
    try:
        return status, json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, None


def escape_token(token: str) -> str:
    """HTML-escape `&`/`<`, and neutralise `@` with a zero-width joiner -- a user-supplied token
    must never render as a live mention or break the table it's embedded in."""
    escaped = token.replace("&", "&amp;").replace("<", "&lt;")
    return escaped.replace("@", "@‍")


def resolve_display_key(model_token: Optional[str], models: dict) -> str:
    """The allowlist key to show for an attempted MODEL token -- it may already be a key, an
    exact listed model value (reverse-looked-up to its key), or (if empty) the default."""
    if not model_token:
        return models["default_model"]
    if model_token in models["models"]:
        return model_token
    for key, entry in models["models"].items():
        if entry["model"] == model_token:
            return key
    return model_token


def describe_reason(reason: str, attempted_model: str, attempted_effort: str, models: dict) -> str:
    if reason == "command:model":
        return f"Unsupported model `{escape_token(attempted_model)}`."
    if reason == "command:effort":
        key = resolve_display_key(attempted_model, models)
        return f"Unsupported effort `{escape_token(attempted_effort)}` for `{escape_token(key)}`."
    if reason == "command:grammar":
        return "Too many arguments -- `@review` takes at most a MODEL and an EFFORT."
    return "Unrecognized `@review` command."


def render_reply(reason: str, attempted_model: str, attempted_effort: str, models: dict) -> str:
    lines = [
        describe_reason(reason, attempted_model, attempted_effort, models),
        "",
        "**Usage:** `@review [MODEL] [EFFORT]`",
        "",
        "| key | model | backend | efforts | default effort |",
        "|---|---|---|---|---|",
    ]
    for key, entry in models["models"].items():
        backend = entry["backend"]
        efforts = ", ".join(models["efforts"][backend])
        default_effort = models["default_effort"][backend]
        lines.append(
            f"| `{escape_token(key)}` | `{escape_token(entry['model'])}` | "
            f"{escape_token(backend)} | {efforts} | {default_effort} |"
        )
    lines.append("")
    lines.append(f"Default model: `{escape_token(models['default_model'])}`")
    return "\n".join(lines)


def find_existing_help_comment(
    http: HttpFn, token: str, repository: str, number: str, marker: str, bot_login: str
) -> Optional[int]:
    """Pages through the PR's comments (same convention as trigger.py's idempotency check),
    matching the marker ONLY on comments the bot itself authored."""
    page = 1
    while True:
        status, body = http(
            "GET", f"/repos/{repository}/issues/{number}/comments?per_page={COMMENTS_PAGE_SIZE}&page={page}", token, None
        )
        if status != 200 or body is None:
            return None
        for existing in body:
            if existing.get("user", {}).get("login") != bot_login:
                continue
            if marker in existing.get("body", ""):
                return existing.get("id")
        if len(body) < COMMENTS_PAGE_SIZE:
            return None
        page += 1


def upsert_help_reply(
    http: HttpFn,
    token: str,
    repository: str,
    number: str,
    comment_id: str,
    reason: str,
    attempted_model: str,
    attempted_effort: str,
    bot_login: str,
    models: dict,
    dry_run: bool,
) -> None:
    marker = HELP_MARKER_TEMPLATE.format(comment_id=comment_id)
    body = f"{marker}\n{render_reply(reason, attempted_model, attempted_effort, models)}"
    existing_id = find_existing_help_comment(http, token, repository, number, marker, bot_login)

    if dry_run:
        action = "update" if existing_id else "create"
        print(json.dumps({"would": action, "repository": repository, "pr_number": number}), file=sys.stderr)
        return

    if existing_id:
        http("PATCH", f"/repos/{repository}/issues/comments/{existing_id}", token, {"body": body})
    else:
        http("POST", f"/repos/{repository}/issues/{number}/comments", token, {"body": body})


def main() -> None:
    _reason, attempted_model, attempted_effort, repository, number, comment_id, _requester = sys.argv[1:8]
    token = os.environ["GH_TOKEN"]
    bot_login = os.environ["BOARD_APP_BOT_LOGIN"]
    dry_run = os.environ.get("DRY_RUN", "1") != "0"
    upsert_help_reply(
        http_request, token, repository, number, comment_id,
        _reason, attempted_model, attempted_effort, bot_login, load_models(), dry_run,
    )


if __name__ == "__main__":
    main()
