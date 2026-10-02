"""Re-validates the handler's `backend`/`model`/`effort` inputs against this
checkout's gateway/models.json (contract §8) -- the handler trusts nothing
from the router's inputs for this choice; it is the allowlist's second,
independent check, same as every other re-validated input."""
import json
import sys
from dataclasses import dataclass


class ModelChoiceError(Exception):
    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolvedModel:
    backend: str
    model: str
    effort: str


def load_models(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _resolve_model(models_data: dict, model: str):
    models = models_data["models"]
    if not model:
        model = models_data["default_model"]
    if model in models:
        entry = models[model]
        return entry["backend"], entry["model"]
    for entry in models.values():
        if entry["model"] == model:
            return entry["backend"], entry["model"]
    raise ModelChoiceError("command:model", f"unlisted model: {model}")


def _resolve_effort(models_data: dict, backend: str, effort: str) -> str:
    if not effort:
        return models_data["default_effort"][backend]
    if effort not in models_data["efforts"].get(backend, []):
        raise ModelChoiceError("command:effort", f"unlisted effort {effort!r} for backend {backend!r}")
    return effort


def resolve(models_data: dict, model: str, effort: str) -> ResolvedModel:
    backend, resolved_model = _resolve_model(models_data, model)
    resolved_effort = _resolve_effort(models_data, backend, effort)
    return ResolvedModel(backend=backend, model=resolved_model, effort=resolved_effort)


def main():
    models_path, backend_in, model_in, effort_in = sys.argv[1:5]
    models_data = load_models(models_path)
    try:
        resolved = resolve(models_data, model_in, effort_in)
    except ModelChoiceError as e:
        sys.exit(f"{e.reason}: {e}")
    if backend_in and backend_in != resolved.backend:
        sys.exit(f"command:model: backend mismatch: input {backend_in!r} vs resolved {resolved.backend!r}")
    print(json.dumps({"backend": resolved.backend, "model": resolved.model, "effort": resolved.effort}))


if __name__ == "__main__":
    main()
