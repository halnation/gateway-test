"""The idempotency marker the AI review handler posts and the router looks for. One definition, imported by both."""

def ai_review_marker(comment_id: str) -> str:
    return f"<!-- board-app:ai-review comment:{comment_id} -->"
