"""Renders github-workflows' ai.py review output into the PR comment body and
upserts it on the target PR (contract §4/§7, the `post` job).

Not reusing github-workflows' render_ai_comment.py (checked at the pinned SHA
80beb9702b828587003f1a9c70fec4edf5efb105): its render_review() bakes in a
different header/footer (`build_header`/`_footer`, keyed off a PR number) and
a different sanitize_markdown() pipeline (zero-width-SPACE mention
neutralizing, image stripping, link-host allowlisting) than this ticket's
contract-mandated header ("AI review (advisory...) for <sha>, requested by
@<login>") and escaping rules (HTML-escape `&`/`<`, zero-width JOINER after
`@`). Grafting our header onto its return value would mean string surgery on
rendered markdown; re-implementing against this ticket's own shape is the
correct-sized duplicate the contract calls for ("Each ticket adds its OWN
files").
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

from marker import ai_review_marker

FIELD_CAP = 300
NOTE_CAP = 500
MAX_MODEL_CONTENT_CHARS = 20000
STATUS_ICON = {"delivered": "✅", "partial": "🟡", "missing": "❌", "unverifiable": "❔"}
ZERO_WIDTH_JOINER = "‍"
_MENTION_RE = re.compile(r"@(?=\S)")


def neutralize_mentions(text: str) -> str:
    """Insert a zero-width joiner after every `@` so GitHub never renders it
    as a live mention, regardless of what follows (a username, `claude`, or
    an email-like `a@b.c`)."""
    return _MENTION_RE.sub("@" + ZERO_WIDTH_JOINER, text)


def _escape_html_specials(s: str) -> str:
    """Escape `&` and `<` so the model's own text can never (a) form a live
    HTML comment -- e.g. a fake `<!-- board-app:ai-review comment:N -->`
    marker embedded in a finding, which would otherwise confuse the upsert's
    marker search -- or (b) smuggle a numeric-character-reference mention
    (`&#64;user` renders as `@user` once GitHub parses the entity, bypassing
    a plain `@`-prefix neutralizer)."""
    return s.replace("&", "&amp;").replace("<", "&lt;")


def _clean(value, cap: int) -> str:
    if value is None:
        return ""
    return _escape_html_specials(str(value))[:cap]


def _table_cell(value, cap: int) -> str:
    """A markdown table cell value must have no literal `|` (it would split
    the cell) and no newline (it would break the row); both are flattened
    in addition to the usual HTML-escaping."""
    cleaned = _clean(value, cap)
    return cleaned.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def parse_ai_output(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _fmt_finding(f: dict) -> str:
    file_ = _clean(f.get("file"), 200)
    line = _clean(f.get("line"), 40)
    loc = f"{file_}:{line}" if file_ else (line or "?")
    defect = _clean(f.get("defect"), FIELD_CAP)
    matches = _clean(f.get("matches"), 200)
    text = f"{loc} — {defect}"
    if matches:
        text += f" — {matches}"
    if f.get("uncertain"):
        text += " (uncertain)"
    return text


def render_findings_markdown(data: dict) -> str:
    lines = []

    deliverables = [d for d in (data.get("deliverables") or []) if isinstance(d, dict)]
    if deliverables:
        lines.append("| Deliverable | Status | Note |")
        lines.append("|---|---|---|")
        for d in deliverables:
            status = d.get("status")
            icon = STATUS_ICON.get(status, "❔")
            item = _table_cell(d.get("item"), FIELD_CAP)
            note = _table_cell(d.get("note"), NOTE_CAP)
            lines.append(f"| {item} | {icon} {_table_cell(status, 20)} | {note} |")
        lines.append("")

    findings = [f for f in (data.get("findings") or []) if isinstance(f, dict)]
    blockers = [f for f in findings if f.get("severity") == "blocker"]
    should_fix = [f for f in findings if f.get("severity") == "should-fix"]
    nits = [f for f in findings if f.get("severity") == "nit"]

    if blockers:
        lines.append("> [!CAUTION]")
        for f in blockers:
            lines.append(f"> 🔴 {_fmt_finding(f)}")
        lines.append("")

    if should_fix:
        lines.append("**Should-fix**")
        for f in should_fix:
            lines.append(f"- 🟠 {_fmt_finding(f)}")
        lines.append("")

    if nits:
        lines.append("<details><summary>Nits</summary>")
        lines.append("")
        for f in nits:
            lines.append(f"- {_fmt_finding(f)}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    more_count = data.get("more_count")
    if isinstance(more_count, int) and more_count > 0:
        lines.append(f"_+{more_count} more_")
        lines.append("")

    if not deliverables and not blockers and not should_fix and not nits:
        lines.append("_No findings._")
        lines.append("")

    return "\n".join(lines).rstrip("\n")


def marker_for(comment_id) -> str:
    return ai_review_marker(comment_id)


def render_model_content(ai_out_text: str) -> str:
    """The MODEL-derived part of the comment: escaped, mention-neutralized,
    and capped at 20,000 chars. The header and marker are NOT part of this
    cap -- they are added after (contract §7 M4)."""
    data = parse_ai_output(ai_out_text)
    if not isinstance(data, dict) or "deliverables" not in data or "findings" not in data:
        content = (
            "_The AI response could not be parsed as structured output; no "
            "review content was generated for this run._"
        )
    else:
        content = render_findings_markdown(data)
    content = neutralize_mentions(content)
    return content[:MAX_MODEL_CONTENT_CHARS]


def build_comment_body(ai_out_text: str, head_sha: str, requester: str, comment_id, model: str, effort: str) -> str:
    safe_model = _escape_html_specials(model)
    safe_effort = _escape_html_specials(effort)
    header = (
        f"AI review (advisory, not a review or approval) by {safe_model} at {safe_effort} "
        f"for {head_sha[:7]}, requested by @{ZERO_WIDTH_JOINER}{requester}"
    )
    content = render_model_content(ai_out_text)
    return "\n".join([header, "", content]).rstrip("\n") + "\n\n" + marker_for(comment_id) + "\n"


def http_request(method: str, url: str, token: str, body=None):
    """The one injectable HTTP function. `body`, if given, is a dict sent as
    JSON. Returns the parsed JSON response (or None for an empty body)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub API error {e.code} on {method} {url}: {e.read().decode('utf-8', 'replace')[:500]}")
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def list_comments(http_fn, repo: str, pr_number, token: str, per_page: int = 100):
    """Pages through every PR/issue comment with per_page=100 (contract
    §7)."""
    comments = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments?per_page={per_page}&page={page}"
        batch = http_fn("GET", url, token) or []
        comments.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return comments


def find_marker_comment_id(http_fn, repo: str, pr_number, token: str, marker: str, bot_login: str):
    """A marker only counts on a comment actually authored by the Board App
    bot -- otherwise a comment from anyone else that happens to contain our
    marker text (accidentally, or an attacker trying to hijack the upsert
    onto someone else's comment) would be treated as "already posted"."""
    for c in list_comments(http_fn, repo, pr_number, token):
        if marker in (c.get("body") or "") and (c.get("user") or {}).get("login") == bot_login:
            return c.get("id")
    return None


def upsert_comment(http_fn, repo: str, pr_number, token: str, body: str, marker: str, bot_login: str):
    existing_id = find_marker_comment_id(http_fn, repo, pr_number, token, marker, bot_login)
    if existing_id is not None:
        http_fn(
            "PATCH",
            f"https://api.github.com/repos/{repo}/issues/comments/{existing_id}",
            token,
            {"body": body},
        )
        return "updated", existing_id
    created = http_fn(
        "POST",
        f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
        token,
        {"body": body},
    )
    return "created", (created or {}).get("id")


def is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "1") != "0"


def get_bot_login() -> str:
    login = os.environ.get("BOARD_APP_BOT_LOGIN", "").strip()
    if not login:
        sys.exit("BOARD_APP_BOT_LOGIN is required (startup-fatal: no bot login to match markers against)")
    return login


def main():
    out_md_path, head_sha, requester, comment_id, repo, pr_number, model, effort = sys.argv[1:9]
    bot_login = get_bot_login()
    with open(out_md_path, encoding="utf-8") as f:
        ai_out_text = f.read()
    body = build_comment_body(ai_out_text, head_sha, requester, comment_id, model, effort)
    marker = marker_for(comment_id)

    if is_dry_run():
        print(json.dumps({"would": "ai-review-comment", "repo": repo, "pr_number": pr_number, "marker": marker}), file=sys.stderr)
        print(body)
        return

    token = os.environ["GH_TOKEN"]
    action, comment_db_id = upsert_comment(http_request, repo, pr_number, token, body, marker, bot_login)
    print(json.dumps({"action": action, "comment_id": comment_db_id}))


if __name__ == "__main__":
    main()
