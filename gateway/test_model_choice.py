"""Tests for model_choice.py's allowlist re-check against gateway/models.json
(contract §8). Uses the real models.json file on disk as the fixture --
that file IS the spec, not a mock of it."""
import json
import os

import pytest

import model_choice as mod

MODELS_PATH = os.path.join(os.path.dirname(__file__), "models.json")


def _models():
    with open(MODELS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---- accepts listed combos ------------------------------------------------

@pytest.mark.parametrize(
    "model,effort,expected_backend,expected_model",
    [
        ("glm", "low", "openrouter", "z-ai/glm-5.3"),
        ("z-ai/glm-5.3", "high", "openrouter", "z-ai/glm-5.3"),
        ("opus", "medium", "anthropic", "claude-opus-5-5"),
        ("claude-sonnet-5-5", "high", "anthropic", "claude-sonnet-5-5"),
    ],
)
def test_resolve_accepts_listed_combos(model, effort, expected_backend, expected_model):
    resolved = mod.resolve(_models(), model, effort)
    assert resolved.backend == expected_backend
    assert resolved.model == expected_model
    assert resolved.effort == effort


def test_resolve_allowlist_key_resolves_to_its_model_id():
    resolved = mod.resolve(_models(), "opus", "high")
    assert resolved.model == "claude-opus-5-5"
    assert resolved.backend == "anthropic"


def test_resolve_applies_defaults_when_omitted():
    resolved = mod.resolve(_models(), "", "")
    assert resolved.model == "z-ai/glm-5.3"
    assert resolved.backend == "openrouter"
    assert resolved.effort == "high"


def test_resolve_applies_default_effort_per_backend():
    resolved = mod.resolve(_models(), "opus", "")
    assert resolved.backend == "anthropic"
    assert resolved.effort == "high"


# ---- rejects unlisted model/effort -----------------------------------------

def test_resolve_rejects_unlisted_model():
    with pytest.raises(mod.ModelChoiceError) as exc:
        mod.resolve(_models(), "gpt-4o", "high")
    assert exc.value.reason == "command:model"


def test_resolve_rejects_unlisted_effort_for_backend():
    # "xhigh" was dropped from the anthropic effort list (contract §9) and
    # was never valid for openrouter -- now invalid everywhere.
    with pytest.raises(mod.ModelChoiceError) as exc:
        mod.resolve(_models(), "opus", "xhigh")
    assert exc.value.reason == "command:effort"


def test_resolve_rejects_effort_not_in_any_list():
    with pytest.raises(mod.ModelChoiceError) as exc:
        mod.resolve(_models(), "glm", "ludicrous")
    assert exc.value.reason == "command:effort"
