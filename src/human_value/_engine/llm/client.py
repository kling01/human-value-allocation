"""
LLM 客户端 —— 薄封装，只读 config/llm_config.yaml，不含任何业务逻辑。

⚠ 2026-10-09 新增 call_llm_with_usage（Rubric Repair MVP，PRD v3 §5）：
   原 call_llm 在 L49 只取 choices[0].message.content，**把 provider 返回的 usage 直接丢弃**，
   导致全链路无法统计 token。本次以**并列新函数**的方式补上用量回收，
   刻意**不改动 call_llm 的签名与行为**——它有 8 处调用点且被测试 patch
   （tests/test_llm_live.py 直接 patch llm.eval.call_llm），改签名会连带破坏既有测试。
"""

import asyncio
import random
import time
import weakref
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import yaml
from pathlib import Path

# Public distribution: no local YAML or credentials are loaded on import.
# The caller must supply its own settings through PipelineConfig.llm_options.
_cfg = {"api_key": "", "base_url": "", "model": "", "concurrency": 4,
        "n_runs": 1, "max_tokens": 4096, "timeout": 120, "retry_attempts": 2}

HEADERS = {
    "Authorization": f"Bearer {_cfg['api_key']}",
    "Content-Type": "application/json",
}

_RETRY_STATUS = {429, 500, 502, 503, 504}
_NETWORK_LIMITS = weakref.WeakKeyDictionary()


def _network_limit():
    loop = asyncio.get_running_loop()
    if loop not in _NETWORK_LIMITS:
        _NETWORK_LIMITS[loop] = asyncio.Semaphore(20)
    return _NETWORK_LIMITS[loop]


@dataclass
class LLMReply:
    """一次 LLM 请求的完整结果，含 provider 真实 usage。

    usage_source 三态（PRD §5.2 规则 2）：
      - "provider" —— provider 真实返回了 usage
      - "unknown"  —— 没有返回 usage，tokens 为 None（**不是 0**）
    """
    content: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    usage_source: str
    model_id: Optional[str]
    finish_reason: Optional[str] = None
    raw: Optional[dict] = None



async def call_llm(prompt: str, client: httpx.AsyncClient) -> str:
    """Shared request path: evaluation and repair both retain provider usage."""
    reply = await call_llm_with_usage(prompt, client)
    return reply.content


def get_concurrency() -> int:
    return int(_cfg.get("concurrency", 10))


def get_n_runs() -> int:
    return int(_cfg.get("n_runs", 1))


def get_base_url() -> str:
    return _cfg["base_url"].rstrip("/")


def get_model() -> str:
    return _cfg["model"]


def get_http_trust_env() -> bool:
    """Whether this run uses environment/OS proxies; recorded with model settings."""
    from human_value._engine.llm.runtime import current_runtime
    return bool(current_runtime()["options"].get("trust_env", _cfg.get("trust_env", True)))


# ── 带 usage 回收的调用（Rubric Repair MVP）───────────────────────────────────

async def call_llm_with_usage(
    prompt: str,
    client: httpx.AsyncClient,
    *,
    usage_sink: Any = None,
    call_id: str = "",
    kind: str = "judge_llm",
    stage: str = "unknown",
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    version: str = "unknown",
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> LLMReply:
    """与 call_llm 同源的调用，但**回收 provider usage 并写入 token 账本**。

    这是「统一调用收口」：所有真实 provider 请求都从这里或 JEV 适配器经过，
    因此 usage 不会漏记。usage_sink 为 None 时退化为普通调用（离线/旧路径用）。

    base_url / api_key / model 可覆盖，用于把某次实验切到另一个网关，
    **不修改全局 llm_config.yaml**（那是全仓库共享配置）。

    失败语义（PRD §5.2 规则 2）：失败请求也可能被计费 —— 这里在失败时写
    on_fail 且 tokens 传 None（usage_source=unknown），**绝不写 0**。
    """
    from human_value._engine.llm.runtime import current_runtime
    import hashlib
    import json
    import uuid

    runtime = current_runtime()
    opts = {**_cfg, **runtime["options"]}
    for key, value in (("base_url", base_url), ("api_key", api_key), ("model", model),
                       ("max_tokens", max_tokens), ("timeout", timeout),
                       ("temperature", temperature)):
        if value is not None:
            opts[key] = value
    model_id = opts["model"]
    url_base = opts["base_url"].rstrip("/")
    endpoint = url_base + ("/chat/completions" if url_base.endswith("/v1") else "/v1/chat/completions")
    headers = {"Authorization": f"Bearer {opts['api_key']}", "Content-Type": "application/json"}
    sink = usage_sink if usage_sink is not None else runtime["sink"]
    tags = {**runtime["tags"]}
    tags.update(kind=kind if kind != "judge_llm" else tags.get("kind", kind),
                stage=stage if stage != "unknown" else tags.get("stage", stage),
                version=version if version != "unknown" else tags.get("version", version))
    tags.pop("model_id", None)
    messages = []
    if opts.get("system_prompt"):
        messages.append({"role": "system", "content": opts["system_prompt"]})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model_id, "messages": messages, "max_tokens": opts.get("max_tokens", 512)}
    for option in ("temperature", "response_format"):
        if opts.get(option) is not None:
            payload[option] = opts[option]
    request_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    root_call_id = call_id or "llm-" + uuid.uuid4().hex
    # One initial request and at most one retry, each independently accounted.
    attempts = min(2, max(1, int(opts.get("retry_attempts", 2))))
    for attempt in range(attempts):
        attempt_id = root_call_id if attempt == 0 else f"{root_call_id}-retry-{attempt}"
        info = {**tags, "attempt": attempt + 1, "request_hash": request_hash}
        if sink is not None:
            if hasattr(sink, "check_before_call"):
                sink.check_before_call(estimated_input_tokens=_estimate_tokens(json.dumps(messages, ensure_ascii=False)))
            sink.on_start(attempt_id, model_id=model_id,
                          estimated_input_tokens=_estimate_tokens(json.dumps(messages, ensure_ascii=False)), **info)
            if hasattr(sink, "on_request"):
                sink.on_request(attempt_id, payload)
        data = {}
        started = time.perf_counter()
        try:
            async with _network_limit():
                resp = await client.post(endpoint, headers=headers, json=payload,
                                         timeout=opts.get("timeout", 80))
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if sink is not None and hasattr(sink, "on_response"):
                sink.on_response(attempt_id, data, status_code=resp.status_code)
            resp.raise_for_status()
            content = data["choices"][0]["message"].get("content") or ""
            finish = data["choices"][0].get("finish_reason")
            usage = data.get("usage") or {}
            inp, outp = usage.get("prompt_tokens"), usage.get("completion_tokens")
            source = "provider" if inp is not None or outp is not None else "unknown"
            actual_model = data.get("model") or model_id
            if sink is not None:
                sink.on_complete(attempt_id, input_tokens=inp, output_tokens=outp,
                                 usage_source=source, model_id=actual_model,
                                 requested_model=model_id, finish_reason=finish,
                                 latency_ms=round((time.perf_counter() - started) * 1000), **info)
            return LLMReply(content, inp, outp, source, actual_model, finish, data)
        except Exception as exc:
            usage = data.get("usage") or {} if isinstance(data, dict) else {}
            inp, outp = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if sink is not None:
                sink.on_fail(attempt_id, error_type=type(exc).__name__, model_id=model_id,
                             input_tokens=inp, output_tokens=outp,
                             usage_source="provider" if inp is not None or outp is not None else "unknown",
                             latency_ms=round((time.perf_counter() - started) * 1000), **info)
            transient = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
            if isinstance(exc, httpx.HTTPStatusError):
                transient = exc.response.status_code in _RETRY_STATUS
            if not transient or attempt + 1 == attempts:
                raise
            await asyncio.sleep(2.0 * (2 ** attempt) + random.uniform(0, 0.5))
    raise RuntimeError("LLM request exhausted")


def _estimate_tokens(prompt: str) -> Optional[int]:
    """粗略估算 input tokens，**仅用于进行中预测**，不计入真实总量。

    刻意用保守的字符比例而不是引入 tokenizer 依赖——估算只出现在
    started.estimated_input_tokens 字段，聚合时不会混入实际 token。
    中文/英文混排按 3 chars/token 粗算，这是启发式而非精确值。
    """
    try:
        return max(1, len(prompt) // 3)
    except (TypeError, AttributeError):
        return None
