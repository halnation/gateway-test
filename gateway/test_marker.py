"""Tests for marker.py -- asserted against the literal string, since this
module IS the single source of truth for the marker format (there is no
independent oracle to compare against; the literal is the spec)."""
from marker import ai_review_marker


def test_marker_is_the_exact_literal_string():
    assert ai_review_marker("123") == "<!-- board-app:ai-review comment:123 -->"


def test_marker_embeds_the_given_comment_id():
    assert ai_review_marker("42") == "<!-- board-app:ai-review comment:42 -->"
    assert ai_review_marker("999") == "<!-- board-app:ai-review comment:999 -->"
