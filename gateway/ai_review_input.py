"""Builds input.txt for github-workflows' ai.py in the review format it
expects: a <linked-issue> block followed by a <pr-diff> block. Ports the
lock-file / vendor-path filter from github-workflows/.github/workflows/
ai-comment.yml's awk script (L159-183) line for line, so a diff touching a
lockfile, a vendor directory (lib/broadcast/out/cache/node_modules) or a
large dependency snapshot gets summarized instead of pasted in full.
"""
import re

_SKIP_DIR_RE = re.compile(r"(^|/)(lib|broadcast|out|cache|node_modules)/")
_SKIP_LOCKFILE_RE = re.compile(r"\.lock$")
_SKIP_NAMED_LOCKFILE_RE = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$")


def _should_skip(path: str) -> bool:
    return bool(
        _SKIP_DIR_RE.search(path)
        or _SKIP_LOCKFILE_RE.search(path)
        or _SKIP_NAMED_LOCKFILE_RE.search(path)
    )


def filter_diff(diff_text: str) -> str:
    """Port of the awk filter in ai-comment.yml: for each `diff --git` hunk,
    either keep it verbatim or replace it with a one-line "omitted" summary
    carrying its changed-line count."""
    blocks = []
    path = None
    buf = []
    count = 0

    def flush():
        if path is None:
            return
        if _should_skip(path):
            blocks.append(f"({path}: {count} lines changed, omitted)")
        else:
            blocks.append("\n".join(buf))

    for line in diff_text.split("\n"):
        if line.startswith("diff --git "):
            flush()
            parts = line.split()
            path = parts[2] if len(parts) > 2 else ""
            if path.startswith("a/"):
                path = path[2:]
            buf = [line]
            count = 0
            continue
        buf.append(line)
        if re.match(r"^[+-]", line) and not line.startswith("+++") and not line.startswith("---"):
            count += 1
    flush()

    return "\n".join(blocks)


def build_input(diff_text: str, linked_issue_body: str = "") -> str:
    issue_section = linked_issue_body if linked_issue_body.strip() else "(no linked issue)"
    parts = [
        "<linked-issue>",
        issue_section,
        "</linked-issue>",
        "<pr-diff>",
        filter_diff(diff_text),
        "</pr-diff>",
        "",
    ]
    return "\n".join(parts)


def main():
    import sys

    diff_path, out_path = sys.argv[1], sys.argv[2]
    with open(diff_path, encoding="utf-8") as f:
        diff_text = f.read()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(build_input(diff_text))


if __name__ == "__main__":
    main()
