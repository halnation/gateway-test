import json
import unittest

import help_reply
import trigger

REPOSITORY = "TokenLogic-com-au/board-app"
NUMBER = "42"
COMMENT_ID = "9001"
BOT_LOGIN = "board-app[bot]"
MODELS = trigger.load_models()


class FakeHttp:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method: str, path: str, token: str, body=None):
        self.calls.append((method, path, body))
        return self.responses.get((method, path), (404, None))


LOOKUP_PATH = f"/repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100&page=1"


class RenderReplyTests(unittest.TestCase):
    def test_unsupported_effort_names_the_effort_and_lists_glm_efforts(self):
        text = help_reply.render_reply("command:effort", "glm", "max", MODELS)
        self.assertIn("`max`", text)
        self.assertIn("`glm`", text)
        self.assertIn("low, medium, high", text)

    def test_unsupported_model_lists_all_models(self):
        text = help_reply.render_reply("command:model", "gpt-9", "", MODELS)
        self.assertIn("`gpt-9`", text)
        self.assertIn("`glm`", text)
        self.assertIn("`opus`", text)
        self.assertIn("`sonnet`", text)
        self.assertIn("z-ai/glm-5.3", text)
        self.assertIn("claude-opus-5-5", text)
        self.assertIn("claude-sonnet-5-5", text)

    def test_user_supplied_token_is_escaped_and_neutralised(self):
        # §7/§10 convention: HTML-escape `&` and `<` only (not `>`); neutralise `@` mentions.
        text = help_reply.render_reply("command:model", "<img>@someone", "", MODELS)
        self.assertNotIn("<img>", text)
        self.assertIn("&lt;img>", text)
        self.assertNotIn("@someone", text)
        self.assertIn("@‍someone", text)


class UpsertHelpReplyTests(unittest.TestCase):
    def test_no_existing_comment_posts_a_new_one(self):
        http = FakeHttp({(("GET", LOOKUP_PATH)): (200, [])})
        help_reply.upsert_help_reply(
            http, "tok", REPOSITORY, NUMBER, COMMENT_ID,
            "command:model", "gpt-9", "", BOT_LOGIN, MODELS, dry_run=False,
        )
        methods = [call[0] for call in http.calls]
        self.assertEqual(methods, ["GET", "POST"])
        post_path = http.calls[1][1]
        self.assertEqual(post_path, f"/repos/{REPOSITORY}/issues/{NUMBER}/comments")
        posted_body = http.calls[1][2]["body"]
        self.assertIn(f"<!-- board-app:ai-review-help comment:{COMMENT_ID} -->", posted_body)

    def test_existing_marker_from_bot_updates_not_creates(self):
        marker = f"<!-- board-app:ai-review-help comment:{COMMENT_ID} -->"
        existing = [{"id": 555, "user": {"login": BOT_LOGIN}, "body": f"{marker}\nold text"}]
        http = FakeHttp({("GET", LOOKUP_PATH): (200, existing)})
        help_reply.upsert_help_reply(
            http, "tok", REPOSITORY, NUMBER, COMMENT_ID,
            "command:model", "gpt-9", "", BOT_LOGIN, MODELS, dry_run=False,
        )
        methods = [call[0] for call in http.calls]
        self.assertEqual(methods, ["GET", "PATCH"])
        patch_path = http.calls[1][1]
        self.assertEqual(patch_path, f"/repos/{REPOSITORY}/issues/comments/555")

    def test_marker_from_non_bot_user_is_ignored_so_a_new_comment_is_posted(self):
        marker = f"<!-- board-app:ai-review-help comment:{COMMENT_ID} -->"
        spoofed = [{"id": 999, "user": {"login": "random-user"}, "body": f"{marker}\nspoofed"}]
        http = FakeHttp({("GET", LOOKUP_PATH): (200, spoofed)})
        help_reply.upsert_help_reply(
            http, "tok", REPOSITORY, NUMBER, COMMENT_ID,
            "command:model", "gpt-9", "", BOT_LOGIN, MODELS, dry_run=False,
        )
        methods = [call[0] for call in http.calls]
        self.assertEqual(methods, ["GET", "POST"])

    def test_dry_run_makes_no_write_call(self):
        http = FakeHttp({("GET", LOOKUP_PATH): (200, [])})
        help_reply.upsert_help_reply(
            http, "tok", REPOSITORY, NUMBER, COMMENT_ID,
            "command:model", "gpt-9", "", BOT_LOGIN, MODELS, dry_run=True,
        )
        methods = [call[0] for call in http.calls]
        self.assertEqual(methods, ["GET"])


if __name__ == "__main__":
    unittest.main()
