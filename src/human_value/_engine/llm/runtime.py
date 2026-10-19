"""Task-local model settings and observations shared by evaluation and replay.

Context variables isolate concurrent requests without editing global YAML.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit

_CURRENT = ContextVar("agent_eval_model_runtime", default=None)
_UNSET = object()
OPTION_KEYS = {"base_url", "api_key", "model", "timeout", "max_tokens", "temperature",
               "retry_attempts", "response_format", "system_prompt", "trust_env"}


def current_runtime() -> dict:
    return _CURRENT.get() or {"options": {}, "sink": None, "tags": {}}


@contextmanager
def model_runtime(*, options=None, usage_sink=_UNSET, **tags):
    parent = current_runtime()
    options = options or {}
    unknown = set(options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"未知模型运行参数：{sorted(unknown)}")
    state = {
        "options": {**parent["options"], **options},
        "sink": parent["sink"] if usage_sink is _UNSET else usage_sink,
        "tags": {**parent["tags"], **tags},
    }
    token = _CURRENT.set(state)
    try:
        yield state
    finally:
        _CURRENT.reset(token)


def public_model_config(config: dict) -> dict:
    """A reproducibility record that never includes credentials."""
    out = {k: deepcopy(v) for k, v in config.items()
           if k in OPTION_KEYS - {"api_key", "system_prompt"}}
    if out.get("base_url"):
        u = urlsplit(out["base_url"])
        host = u.hostname or ""
        if u.port:
            host += ":" + str(u.port)
        out["base_url"] = urlunsplit((u.scheme, host, u.path, "", ""))
    return out
