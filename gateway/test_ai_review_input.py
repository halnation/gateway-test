"""Tests for ai_review_input.py.

The parity test's oracle is the ACTUAL awk script copied verbatim from
github-workflows/.github/workflows/ai-comment.yml (L159-183, the `diff.txt`
filter), run via subprocess -- not our Python port. That way the test
exercises an independent implementation of the same rule, instead of
re-deriving the expected output from the code under test.
"""
import subprocess
import textwrap

from ai_review_input import build_input, filter_diff

# Verbatim awk program from github-workflows' .github/workflows/ai-comment.yml
# L159-183, at the pinned GW_REF SHA (80beb9702b828587003f1a9c70fec4edf5efb105,
# see .github/workflows/ai-review.yml's env.GW_REF).
_AWK_FILTER = r"""
function flush() {
  if (path != "") {
    skip = (path ~ /(^|\/)(lib|broadcast|out|cache|node_modules)\//) \
        || (path ~ /\.lock$/) \
        || (path ~ /(^|\/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$/)
    if (skip) print "(" path ": " count " lines changed, omitted)"
    else print buf
  }
}
/^diff --git / {
  flush()
  path = $3
  sub(/^a\//, "", path)
  buf = $0
  count = 0
  next
}
{
  buf = buf "\n" $0
  if ($0 ~ /^[+-]/ && $0 !~ /^(\+\+\+|---)/) count++
  next
}
END { flush() }
"""

SAMPLE_DIFF = textwrap.dedent(
    """\
    diff --git a/src/main.py b/src/main.py
    index 1111111..2222222 100644
    --- a/src/main.py
    +++ b/src/main.py
    @@ -1,2 +1,3 @@
     def main():
    -    pass
    +    print("hi")
    +    return 0
    diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
    index 3333333..4444444 100644
    --- a/pnpm-lock.yaml
    +++ b/pnpm-lock.yaml
    @@ -1,1 +1,1 @@
    -old
    +new
    diff --git a/node_modules/leftpad/index.js b/node_modules/leftpad/index.js
    index 5555555..6666666 100644
    --- a/node_modules/leftpad/index.js
    +++ b/node_modules/leftpad/index.js
    @@ -1,1 +1,2 @@
     module.exports = {}
    +module.exports.extra = 1
    """
)


def _awk_oracle(diff_text: str) -> str:
    result = subprocess.run(
        ["awk", _AWK_FILTER],
        input=diff_text,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.rstrip("\n")


def test_filter_diff_matches_awk_oracle_on_mixed_diff():
    expected = _awk_oracle(SAMPLE_DIFF)
    actual = filter_diff(SAMPLE_DIFF).rstrip("\n")
    assert actual == expected
    # Sanity: the oracle actually distinguishes the three cases, otherwise
    # this test would pass vacuously.
    assert "pnpm-lock.yaml: 2 lines changed, omitted" in expected
    assert "node_modules/leftpad/index.js: 1 lines changed, omitted" in expected
    assert "def main():" in expected
    assert "print(\"hi\")" in expected


def test_filter_diff_keeps_normal_file_in_full():
    diff_text = textwrap.dedent(
        """\
        diff --git a/README.md b/README.md
        index 1..2 100644
        --- a/README.md
        +++ b/README.md
        @@ -1 +1 @@
        -old line
        +new line
        """
    )
    out = filter_diff(diff_text)
    assert "diff --git a/README.md b/README.md" in out
    assert "-old line" in out
    assert "+new line" in out
    assert "omitted" not in out


def test_build_input_wraps_sections():
    out = build_input(SAMPLE_DIFF, "Deliverable: ship it")
    assert out.startswith("<linked-issue>\nDeliverable: ship it\n</linked-issue>\n<pr-diff>\n")
    assert out.rstrip("\n").endswith("</pr-diff>")


def test_build_input_no_linked_issue_placeholder():
    out = build_input(SAMPLE_DIFF, "")
    assert "<linked-issue>\n(no linked issue)\n</linked-issue>" in out
