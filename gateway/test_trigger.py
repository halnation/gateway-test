import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import trigger

OWNER = "TokenLogic-com-au"
REPO = "board-app"
REPOSITORY = f"{OWNER}/{REPO}"
NUMBER = "42"
COMMENT_ID = "9001"
DELIVERY_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
LOGIN = "alice"
BOT_LOGIN = "board-app[bot]"
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
CREATED_AT = NOW.isoformat().replace("+00:00", "Z")
# Literal, hand-encoded -- not computed via urllib.parse.quote, so this isn't an oracle test.
ENCODED_CREATED_AT = "2026-10-01T12%3A00%3A00Z"
PAGE_SIZE = 100  # literal, matching the contract's per_page=100 -- not trigger.COMMENTS_PAGE_SIZE

COMMENTS_PATH = (
    f"/repos/{REPOSITORY}/issues/{NUMBER}/comments"
    f"?since={ENCODED_CREATED_AT}&per_page={PAGE_SIZE}&page=1"
)
COMMENTS_PATH_PAGE2 = (
    f"/repos/{REPOSITORY}/issues/{NUMBER}/comments"
    f"?since={ENCODED_CREATED_AT}&per_page={PAGE_SIZE}&page=2"
)


def base_inputs(**overrides) -> dict:
    inputs = {
        "event": "issue_comment",
        "repository": REPOSITORY,
        "number": NUMBER,
        "comment_id": COMMENT_ID,
        "delivery_id": DELIVERY_ID,
    }
    inputs.update(overrides)
    return inputs


def comment_body(body="@review", created_at=None, login=LOGIN) -> dict:
    return {
        "issue_url": f"https://api.github.com/repos/{REPOSITORY}/issues/{NUMBER}",
        "body": body,
        "created_at": (created_at or CREATED_AT),
        "user": {"login": login},
    }


def pr_body(state="open", private=True, sha="deadbeef") -> dict:
    return {
        "state": state,
        "head": {"sha": sha},
        "base": {"repo": {"private": private}},
    }


class FakeHttp:
    """Maps (method, path) -> (status, body). Missing keys 404 with no body."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, token: str):
        self.calls.append((method, path))
        return self.responses.get((method, path), (404, None))


def fixed_clock(when=NOW):
    return lambda: when


def default_responses(overrides: dict | None = None):
    responses = {
        ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (200, comment_body()),
        ("GET", f"/repos/{REPOSITORY}/pulls/{NUMBER}"): (200, pr_body()),
        ("GET", f"/orgs/{OWNER}/members/{LOGIN}"): (204, None),
        ("GET", COMMENTS_PATH): (200, []),
    }
    responses.update(overrides or {})
    return responses


class VerifyTests(unittest.TestCase):
    def run_verify(self, inputs=None, responses=None, clock=None, repo_owner=OWNER, bot_login=BOT_LOGIN):
        http = FakeHttp(responses if responses is not None else default_responses())
        return trigger.verify(
            inputs or base_inputs(), "tok", http, clock or fixed_clock(), repo_owner, bot_login
        )

    # -- full pass --

    def test_full_pass(self):
        result = self.run_verify()
        self.assertEqual(result["verified"], "true")
        self.assertEqual(result["repository"], REPOSITORY)
        self.assertEqual(result["pr_number"], NUMBER)
        self.assertEqual(result["head_sha"], "deadbeef")
        self.assertEqual(result["comment_id"], COMMENT_ID)
        self.assertEqual(result["command"], "review")
        self.assertEqual(result["requester"], LOGIN)
        # §8 defaults: @review alone resolves to glm / openrouter / z-ai/glm-5.3 / high.
        self.assertEqual(result["backend"], "openrouter")
        self.assertEqual(result["model"], "z-ai/glm-5.3")
        self.assertEqual(result["effort"], "high")

    def test_requester_comes_from_api_not_dispatch_inputs(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200,
                comment_body(login="api-sourced-login"),
            ),
            ("GET", f"/orgs/{OWNER}/members/api-sourced-login"): (204, None),
        })
        inputs = base_inputs()
        self.assertNotIn("api-sourced-login", json.dumps(inputs))
        result = self.run_verify(inputs=inputs, responses=responses)
        self.assertEqual(result["verified"], "true")
        self.assertEqual(result["requester"], "api-sourced-login")

    # -- one test per check, each failing --

    def test_check1_comment_not_found(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (404, None),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "comment:not_found")

    def test_check1_issue_url_mismatch(self):
        mismatched = comment_body()
        mismatched["issue_url"] = f"https://api.github.com/repos/{REPOSITORY}/issues/999"
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (200, mismatched),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "comment:issue_mismatch")

    def test_check1_issue_url_must_match_exactly_not_just_suffix(self):
        # A URL that merely ENDS with the right suffix (e.g. a different repo that happens to
        # share a numeric suffix) must be rejected -- only an exact match passes.
        mismatched = comment_body()
        mismatched["issue_url"] = f"https://api.github.com/repos/evil/{REPO}/issues/{NUMBER}"
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (200, mismatched),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "comment:issue_mismatch")

    def test_check2_not_a_pr(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/pulls/{NUMBER}"): (404, None),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "pr:not_found")

    def test_check2_pr_not_open(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/pulls/{NUMBER}"): (200, pr_body(state="closed")),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "pr:not_open")

    def test_check3_command_does_not_match(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="just a regular comment"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:no_match")

    def test_check3_old_trigger_word_is_not_a_command(self):
        # §8 overrides §7: the trigger word is now `@review`, not `@claude review`.
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@claude review"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:no_match")

    def test_check3_four_tokens_is_not_a_command(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review opus high extra"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:no_match")

    def test_check3_command_must_be_first_line(self):
        # Only the FIRST line counts, trimmed -- trailing lines never rescue (or poison) a
        # non-matching first line.
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="hello\n@review"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:no_match")

    def test_check3_command_first_line_with_trailing_lines_passes(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review\nplease be thorough"),
            ),
        })
        result = self.run_verify(responses=responses)
        self.assertEqual(result["verified"], "true")

    def test_check3_vertical_tab_splits_first_line_like_the_relay(self):
        # §9: the first line is taken via splitlines(), the same as the relay -- splitlines()
        # treats \x0b (vertical tab) as a line break too, unlike split("\n"), so anything after
        # it is a second "line" and never reaches the token parse.
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review\x0bnot part of the command"),
            ),
        })
        result = self.run_verify(responses=responses)
        self.assertEqual(result["verified"], "true")
        self.assertEqual(result["backend"], "openrouter")
        self.assertEqual(result["model"], "z-ai/glm-5.3")

    def test_check3_empty_body_is_not_a_command(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body=""),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:no_match")

    def test_check3_model_key_resolves_to_anthropic_opus(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review opus"),
            ),
        })
        result = self.run_verify(responses=responses)
        self.assertEqual(result["backend"], "anthropic")
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["effort"], "high")

    def test_check3_model_key_with_explicit_effort(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review opus high"),
            ),
        })
        result = self.run_verify(responses=responses)
        self.assertEqual(result["backend"], "anthropic")
        self.assertEqual(result["model"], "claude-opus-5-5")
        self.assertEqual(result["effort"], "high")

    def test_check3_max_effort_no_longer_allowed_for_anthropic(self):
        # §9: anthropic's efforts dropped xhigh/max -- "max" is no longer anything's effort.
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review opus max"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:effort")

    def test_check3_exact_model_value_with_explicit_effort(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review z-ai/glm-5.3 low"),
            ),
        })
        result = self.run_verify(responses=responses)
        self.assertEqual(result["backend"], "openrouter")
        self.assertEqual(result["model"], "z-ai/glm-5.3")
        self.assertEqual(result["effort"], "low")

    def test_check3_effort_not_allowed_for_backend(self):
        # "max" is an anthropic-only effort; openrouter's glm doesn't allow it.
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review glm max"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:effort")

    def test_check3_unknown_model_rejected(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body="@review gpt-9"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "command:model")

    def test_check4_not_org_member(self):
        responses = default_responses({
            ("GET", f"/orgs/{OWNER}/members/{LOGIN}"): (404, None),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "membership:not_member")

    def test_check5_stale_comment(self):
        old_created_at = (NOW - timedelta(minutes=16)).isoformat().replace("+00:00", "Z")
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(created_at=old_created_at),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "recency:stale")

    def test_check5_exactly_15_minutes_is_not_stale(self):
        edge_created_at = (NOW - timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(created_at=edge_created_at),
            ),
            ("GET", (
                f"/repos/{REPOSITORY}/issues/{NUMBER}/comments"
                f"?since=2026-10-01T11%3A45%3A00Z&per_page={PAGE_SIZE}&page=1"
            )): (200, []),
        })
        result = self.run_verify(responses=responses)
        self.assertEqual(result["verified"], "true")

    def test_check5_bad_timestamp(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(created_at="not-a-timestamp"),
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "recency:bad_timestamp")

    def test_check6_idempotency_marker_present_page1(self):
        marker = "<!-- board-app:ai-review comment:9001 -->"
        responses = default_responses({
            ("GET", COMMENTS_PATH): (
                200, [{"body": f"some text\n{marker}\nmore", "user": {"login": BOT_LOGIN}}],
            ),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "idempotency:duplicate")

    def test_check6_idempotency_marker_on_page_two(self):
        marker = "<!-- board-app:ai-review comment:9001 -->"
        page1 = [
            {"body": f"filler {i}", "user": {"login": "someone-else"}}
            for i in range(PAGE_SIZE)
        ]
        page2 = [{"body": f"found it\n{marker}", "user": {"login": BOT_LOGIN}}]
        responses = default_responses({
            ("GET", COMMENTS_PATH): (200, page1),
            ("GET", COMMENTS_PATH_PAGE2): (200, page2),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "idempotency:duplicate")

    def test_check6_marker_from_non_bot_user_is_ignored(self):
        marker = "<!-- board-app:ai-review comment:9001 -->"
        responses = default_responses({
            ("GET", COMMENTS_PATH): (
                200, [{"body": f"spoofed marker\n{marker}", "user": {"login": "random-user"}}],
            ),
        })
        result = self.run_verify(responses=responses)
        self.assertEqual(result["verified"], "true")

    def test_check6_idempotency_fetch_failed(self):
        responses = default_responses({
            ("GET", COMMENTS_PATH): (500, None),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "idempotency:fetch_failed")

    def test_check7_public_repo(self):
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/pulls/{NUMBER}"): (200, pr_body(private=False)),
        })
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(responses=responses)
        self.assertEqual(ctx.exception.reason, "visibility:public")

    # -- malformed inputs --

    def test_malformed_number_non_digit(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(number="4a2"))
        self.assertEqual(ctx.exception.reason, "format:number")

    def test_malformed_comment_id_non_digit(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(comment_id="abc"))
        self.assertEqual(ctx.exception.reason, "format:comment_id")

    def test_malformed_repository_regex(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(repository="not-a-valid-repo"))
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_malformed_repository_trailing_slash(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(repository=f"{REPOSITORY}/"))
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_malformed_repository_comma(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(repository=f"{OWNER},{REPO}"))
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_malformed_delivery_id_bad_guid(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(delivery_id="not-a-guid"))
        self.assertEqual(ctx.exception.reason, "format:delivery_id")

    def test_malformed_repository_newline_injection(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(repository=f"{REPOSITORY}\nverified=true"))
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_malformed_event_not_issue_comment(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(event="pull_request"))
        self.assertEqual(ctx.exception.reason, "format:event")

    def test_malformed_repository_another_owner(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(inputs=base_inputs(repository=f"some-other-org/{REPO}"))
        self.assertEqual(ctx.exception.reason, "format:owner_mismatch")

    # -- never echoes the comment body --

    def test_never_echoes_comment_body_on_failure(self):
        secret_body = "@claude review SECRET_TOKEN_SHOULD_NOT_LEAK"
        responses = default_responses({
            ("GET", f"/repos/{REPOSITORY}/issues/comments/{COMMENT_ID}"): (
                200, comment_body(body=secret_body),
            ),
        })
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            open(output_path, "w").close()
            http = FakeHttp(responses)
            trigger.run(
                base_inputs(), "tok", output_path, OWNER, BOT_LOGIN, http=http, clock=fixed_clock()
            )
            with open(output_path) as handle:
                contents = handle.read()
        self.assertNotIn("SECRET_TOKEN_SHOULD_NOT_LEAK", contents)
        self.assertIn("verified=false", contents)
        self.assertIn("reason=command:no_match", contents)

    # -- unexpected / transport-level failures never crash the job --

    def test_network_error_is_a_clean_failure_not_a_crash(self):
        def broken_http(method, path, token):
            raise RuntimeError("boom: network exploded")

        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            open(output_path, "w").close()
            trigger.run(
                base_inputs(), "tok", output_path, OWNER, BOT_LOGIN,
                http=broken_http, clock=fixed_clock(),
            )
            with open(output_path) as handle:
                contents = handle.read()
        self.assertIn("verified=false", contents)
        self.assertIn("reason=error:unexpected", contents)

    # -- run() writes outputs --

    def test_run_writes_success_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            open(output_path, "w").close()
            http = FakeHttp(default_responses())
            trigger.run(
                base_inputs(), "tok", output_path, OWNER, BOT_LOGIN, http=http, clock=fixed_clock()
            )
            with open(output_path) as handle:
                contents = handle.read()
        self.assertIn("verified=true", contents)
        self.assertIn(f"requester={LOGIN}", contents)
        self.assertIn("command=review", contents)

    def test_empty_bot_login_is_fatal_not_a_silent_skip(self):
        # An empty bot login must never be treated as "idempotency doesn't apply" -- it is a
        # config error on its own, checked before any HTTP call that would need it.
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            self.run_verify(bot_login="")
        self.assertEqual(ctx.exception.reason, "config:bot-login")

    def test_empty_bot_login_via_run_writes_clean_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            open(output_path, "w").close()
            http = FakeHttp(default_responses())
            trigger.run(base_inputs(), "tok", output_path, OWNER, "", http=http, clock=fixed_clock())
            with open(output_path) as handle:
                contents = handle.read()
        self.assertEqual(contents, "verified=false\nreason=config:bot-login\n")


class ValidateInputsTests(unittest.TestCase):
    """§7 'Validate inputs BEFORE any mint': format + owner pin, with no HTTP and no token."""

    def test_valid_returns_name(self):
        name = trigger.validate_inputs(base_inputs(), OWNER)
        self.assertEqual(name, REPO)

    def test_trailing_slash_rejected(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(repository=f"{REPOSITORY}/"), OWNER)
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_comma_rejected(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(repository=f"{OWNER},{REPO}"), OWNER)
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_another_owner_rejected(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(repository=f"some-other-org/{REPO}"), OWNER)
        self.assertEqual(ctx.exception.reason, "format:owner_mismatch")

    def test_run_validate_writes_name_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            open(output_path, "w").close()
            trigger.run_validate(base_inputs(), OWNER, output_path)
            with open(output_path) as handle:
                contents = handle.read()
        self.assertIn("valid=true", contents)
        self.assertIn(f"name={REPO}", contents)

    def test_nothing_reaches_github_output_before_validation_decides(self):
        # The file starts completely empty; a failing validation must write its one failure
        # record and nothing else -- never a partial `name=` (or any other key) written before
        # the owner-pin / format decision is made.
        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "github_output")
            open(output_path, "w").close()
            trigger.run_validate(base_inputs(repository=f"some-other-org/{REPO}"), OWNER, output_path)
            with open(output_path) as handle:
                contents = handle.read()
        self.assertEqual(contents, "valid=false\nreason=format:owner_mismatch\n")
        self.assertNotIn("name=", contents)

    def test_unicode_digit_rejected_in_number(self):
        # `\d` in Python's re matches non-ASCII Unicode digits too (e.g. Arabic-Indic U+0660);
        # the number must be ASCII [0-9] only, never something GitHub's own API would reject.
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(number="٠1"), OWNER)
        self.assertEqual(ctx.exception.reason, "format:number")

    def test_unicode_digit_rejected_in_comment_id(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(comment_id="٠1"), OWNER)
        self.assertEqual(ctx.exception.reason, "format:comment_id")

    def test_repo_name_dot_rejected(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(repository=f"{OWNER}/."), OWNER)
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_repo_name_dotdot_rejected(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(repository=f"{OWNER}/.."), OWNER)
        self.assertEqual(ctx.exception.reason, "format:repository")

    def test_repo_owner_dot_rejected(self):
        with self.assertRaises(trigger.VerifyFailure) as ctx:
            trigger.validate_inputs(base_inputs(repository=f"./{REPO}"), OWNER)
        self.assertEqual(ctx.exception.reason, "format:repository")


if __name__ == "__main__":
    unittest.main()
