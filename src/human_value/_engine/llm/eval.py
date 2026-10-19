"""
LLM 评测入口。

用法：
  python -m llm.eval --data ./data/travel_normalized.json --out ./output/travel/
  python -m llm.eval --data ./data/travel_normalized.json --out ./output/travel/ --dry-run

输出：
  output/llm_results.json  —— 每条 trace 的 LLM 评测结果
  （由 report.py 与 rule detail.json 合并生成最终报告）

输出格式（每条 trace）：
  {
    "trace_id":    "abc123",
    "timestamp":   "...",
    "langfuse_url": "...",
    "evaluator":   "TRACE_QUALITY",
    "iterate_over": "trace",
    "n_steps":     1,
    "n_passed":    1,
    "passed":      true,
    "steps": [{"step": 1, "agent_name": "trace", "passed": 1, "reason": "..."}]
  }
"""

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from human_value._engine.llm.client import call_llm, get_concurrency, get_n_runs
from human_value._engine.llm.runtime import model_runtime
from human_value._engine.core.evaluator_spec import OUTPUT_CONTRACTS
from human_value._engine.core.meta import write_meta
from human_value._engine.core.features import extract as _extract_features, get_messages as _get_messages, get_langfuse_url as _get_langfuse_url, get_full_trace_str as _get_full_trace_str, _content_to_str
from human_value._engine.core.rules import load_rules_from_dict, apply_rules

_retriever = None

def _get_retriever():
    global _retriever
    if _retriever is None:
        try:
            from retrieval.retriever import FewshotRetriever
            _retriever = FewshotRetriever()
        except Exception:
            _retriever = False  # sentinel: unavailable
    return _retriever if _retriever else None

_EVALS_PATH = Path(__file__).parent.parent / "config" / "common" / "llm_evals.yaml"


# ── 规则字段依赖声明 ───────────────────────────────────────────────────────────

# LLM prompt 里引用这些占位符，必须有规则评测结果提供值；否则加载时报错
RULE_FIELDS: frozenset = frozenset({"score", "passed", "issues", "issue_detail"})

# 这些字段来自 TraceFeatures，不依赖规则，extract_trace_context 总会填充
FEATURE_FIELDS: frozenset = frozenset({
    "n_env_fail", "n_real_fail", "n_empty_return", "n_success_return",
    "subagent_success_rate", "has_final_output", "is_contaminated",
    "n_dispatch", "agent_names",
})


def get_rule_deps(prompt: str) -> set:
    """返回 prompt 模板中引用的规则字段名集合。"""
    return set(re.findall(r'\{\{(\w+)\}\}', prompt)) & RULE_FIELDS


def has_inline_rules(evaluator: dict) -> bool:
    """evaluator 是否定义了 inline_rules 块。"""
    return bool(evaluator.get("inline_rules"))


def validate_rule_deps(evaluators: list, has_rules: bool) -> None:
    """
    校验 LLM 评估器的规则字段依赖。
    has_rules=False 时，若 evaluator 引用了规则字段且没有 inline_rules，抛出 ValueError。
    """
    errors = []
    for ev in evaluators:
        deps = get_rule_deps(ev.get("prompt", ""))
        if deps and not has_rules and not has_inline_rules(ev):
            errors.append(f"  {ev['name']!r}: 引用了规则字段 {sorted(deps)}")
    if errors:
        raise ValueError(
            "以下 LLM 评估器依赖规则字段，但规则评测未启用且未配置 inline_rules：\n" + "\n".join(errors)
        )


# ── trace 级提取 ──────────────────────────────────────────────────────────────

def extract_trace_context(trace: dict, rule_ctx: dict = None) -> dict:
    """提取 trace 级别评测所需字段。rule_ctx 为该 trace 的规则评测结果（可选）。"""
    msgs = _get_messages(trace)
    dispatch_tool = trace.get("dispatch_tool_name", "dispatch_subagent")

    # 当前被评测轮次：最后一条 human 消息。历史输入不替代当前输入。
    user_input = ""
    last_human = -1
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("type") == "human":
            user_input = _content_to_str(m.get("content"))
            last_human = i
            break

    # 最终输出：最后一条 ai 消息的 content
    final_output = ""
    for m in reversed(msgs[last_human + 1:] if last_human >= 0 else msgs):
        if m.get("type") == "ai" and not m.get("tool_calls"):
            content = _content_to_str(m.get("content")).strip()
            if content:
                final_output = content
                break

    # dispatch 摘要：每次调用的 agent + task
    dispatches = []
    for m in msgs:
        if m.get("type") != "ai":
            continue
        for tc in m.get("tool_calls") or []:
            if tc.get("name") != dispatch_tool:
                continue
            args = tc.get("args") or {}
            agent = args.get("agent_name", "?")
            task  = args.get("task", "")[:200]
            dispatches.append(f"- [{agent}] {task}")
    dispatch_summary = "\n".join(dispatches) or "（无 dispatch 调用）"

    # 完整 trace 字符串（含 subagent 内部工具调用，替代纯 messages 序列化）
    messages_str = _get_full_trace_str(trace)

    # 从 TraceFeatures 提取结构特征（不依赖规则，总会填充）
    feat = _extract_features(trace)

    ctx: dict = {
        # 对话内容
        "input":               user_input,
        "output":              final_output,
        "dispatch_summary":    dispatch_summary,
        "messages":            messages_str,
        # 预期输出（可选，用于语义相似度评估；来自 trace["expected_output"]）
        "expected_output":     trace.get("expected_output", "") or "",
        # feature 字段（来自 TraceFeatures，无需规则）
        "n_dispatch":          str(feat.n_dispatch),
        "agent_names":         ", ".join(feat.agent_names) or "（无）",
        "n_env_fail":          str(feat.n_env_fail),
        "n_real_fail":         str(feat.n_real_fail),
        "n_empty_return":      str(feat.n_empty_return),
        "n_success_return":    str(feat.n_success_return),
        "subagent_success_rate": str(feat.subagent_success_rate),
        "has_final_output":    str(feat.has_final_output),
        "is_contaminated":     str(feat.is_env_contaminated),
    }

    # 规则字段（需要规则评测已运行；未运行时置空不报错，校验在 validate_rule_deps 时报错）
    if rule_ctx:
        score        = rule_ctx.get("score", "N/A")
        passed       = rule_ctx.get("passed")
        issues       = rule_ctx.get("issues") or []
        issue_detail = rule_ctx.get("issue_detail") or {}
        ctx.update({
            "score":        str(round(score, 1)) if isinstance(score, (int, float)) else str(score),
            "passed":       "通过" if passed is True else ("未通过" if passed is False else str(passed)),
            "issues":       "\n".join(f"- {i}" for i in issues) or "（无触发规则）",
            "issue_detail": "\n".join(f"- {k}: {v}" for k, v in issue_detail.items()) or "（无）",
        })
    else:
        ctx.update({"score": "", "passed": "", "issues": "", "issue_detail": ""})

    # Annotation adapters preserve explicitly supplied fields. An empty field is
    # a real value; only absence means unavailable. No prompt-keyword guessing.
    supplied = trace.get("evaluation_context") or {}
    system = "\n".join(_content_to_str(m.get("content")) for m in msgs if m.get("type") == "system")
    history = [m for m in msgs[:max(0, last_human)] if m.get("type") != "system"]
    values = {
        "User_Input": user_input if last_human >= 0 else None,
        "Agent_output": final_output,
        "Dialog_history": json.dumps(history, ensure_ascii=False),
        "Expected_Output": trace.get("expected_output"),
        "Metadata": trace.get("metadata"),
        "Trace": messages_str,
        "Agent_config": system or trace.get("agent_config"),
    }
    values.update({k: v for k, v in supplied.items() if k in values})
    for key, value in values.items():
        ctx[key] = ("[未提供]" if value is None else
                    json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))
    ctx["_available_evidence"] = [k for k, v in values.items() if v is not None]
    return ctx


def _inject_fewshot(ctx: dict, evaluator: dict) -> str:
    retriever = _get_retriever()
    if retriever is None:
        return ""
    dim             = evaluator.get("fewshot_eval_dimension", "TASK_COMPLETION")
    top_k           = int(evaluator.get("fewshot_top_k", 3))
    top_k_positive  = evaluator.get("fewshot_top_k_positive")
    top_k_negative  = evaluator.get("fewshot_top_k_negative")
    query  = {
        "user_input":       ctx.get("input", ""),
        "final_output":     ctx.get("output", ""),
        "dispatch_summary": ctx.get("dispatch_summary", ""),
    }
    kwargs = {"eval_dimension": dim}
    if top_k_positive is not None or top_k_negative is not None:
        kwargs["top_k_positive"] = int(top_k_positive) if top_k_positive else None
        kwargs["top_k_negative"] = int(top_k_negative) if top_k_negative else None
    else:
        kwargs["top_k"] = top_k
    try:
        examples = retriever.retrieve(query, **kwargs)
        show_trace = bool(evaluator.get("fewshot_show_trace", False))
        return retriever.format_for_prompt(examples, show_trace=show_trace)
    except Exception:
        return ""


def render_trace_prompt(template: str, ctx: dict) -> str:
    """将 ctx 中所有键值替换进 template 的 {{key}} 占位符。"""
    result = template
    for key, val in ctx.items():
        result = result.replace(f"{{{{{key}}}}}", str(val))
    return result


# ── dispatch step 提取（保留，供后续 dispatch 级评测复用）─────────────────────

def extract_dispatch_steps(trace: dict, context_window: int = 6) -> list:
    """从 normalized trace 里提取每次 dispatch 调用及其上下文。"""
    msgs = _get_messages(trace)
    dispatch_tool = trace.get("dispatch_tool_name", "dispatch_subagent")

    steps, step_idx = [], 0
    for i, msg in enumerate(msgs):
        if msg.get("type") != "ai":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("name") != dispatch_tool:
                continue
            step_idx += 1
            preceding = msgs[max(0, i - context_window): i]
            tool_result = ""
            for j in range(i + 1, len(msgs)):
                if msgs[j].get("type") == "tool":
                    tool_result = msgs[j].get("content", "") or ""
                    break
            steps.append({
                "step_index":    step_idx,
                "agent_name":    (tc.get("args") or {}).get("agent_name", "?"),
                "dispatch_args": tc.get("args") or {},
                "context_msgs":  preceding,
                "tool_result":   tool_result,
            })
    return steps


def format_context(msgs: list) -> str:
    lines = []
    for m in msgs:
        role = {"ai": "Assistant", "human": "User", "tool": "Tool", "system": "System"}.get(m.get("type", ""), "?")
        content = (m.get("content", "") or "")[:300]
        tcs = m.get("tool_calls") or []
        if tcs:
            names = ", ".join(tc.get("name", "") for tc in tcs)
            content = f"[调用: {names}] {content}".strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) or "（无上下文）"


def render_prompt(template: str, step: dict, context_window: int) -> str:
    context_str  = format_context(step["context_msgs"])
    args_str     = json.dumps(step["dispatch_args"], ensure_ascii=False)
    tool_result  = (step["tool_result"] or "（无返回）")[:500]
    return (template
            .replace("{{context}}", context_str)
            .replace("{{context_window}}", str(context_window))
            .replace("{{agent_name}}", step["agent_name"])
            .replace("{{dispatch_args}}", args_str)
            .replace("{{tool_result}}", tool_result))


# ── LLM 输出解析 ──────────────────────────────────────────────────────────────

_LABEL_TO_PASSED = {"合格": 1, "可接受": 1, "不需要调用": 1, "不合格": 0}


def parse_llm_output(raw: str) -> dict:
    """解析 LLM 返回。
    支持三种格式：
      {"output": 1/0/-1, "reason": "...", "confidence": ...}   三态：1=PASS 0=不适用 -1=FAIL
      {"passed": 0/1, "analysis": "...", "reason": "..."}      二值旧格式，不产出 outcome
      {"label": "合格|不合格|可接受|不需要调用", "reason": "..."} 二值旧格式，不产出 outcome

    三态和二值是两套并行语义，见 docs/tristate_rules.md：
      passed 里的 -1（在 eval_step 里赋值）是"没拿到有效答案"的错误哨兵值；
      outcome 里的 -1 是"规则真的判定为 FAIL"，两者不可混用、不可互相 fallback。
    """
    def _from_dict(d: dict) -> dict:
        if "output" in d:
            try:
                outcome = int(d["output"])
            except (TypeError, ValueError):
                outcome = None
            if outcome not in (1, 0, -1):
                outcome = None
            return {
                "passed":   1 if outcome == 1 else 0,  # 向后兼容派生，仅供未升级的旧消费方兜底
                "outcome":  outcome,
                "label":    "",
                "analysis": str(d.get("analysis", "")).strip(),
                "reason":   str(d.get("reason", "")).strip(),
            }
        label = str(d.get("label", "")).strip()
        if label in _LABEL_TO_PASSED:
            return {
                "passed":   _LABEL_TO_PASSED[label],
                "label":    label,
                "analysis": str(d.get("analysis", "")).strip(),
                "reason":   str(d.get("reason", "")).strip(),
            }
        return {
            "passed":   int(bool(d.get("passed"))),
            "label":    label,
            "analysis": str(d.get("analysis", "")).strip(),
            "reason":   str(d.get("reason", "")).strip(),
        }

    # 先尝试整体解析
    try:
        d = json.loads(raw.strip())
        return _from_dict(d)
    except Exception:
        pass
    # 再用贪心正则提取最大 JSON 块（防止 reason 中含 } 被截断）
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            d = json.loads(m.group())
            return _from_dict(d)
    except Exception:
        pass
    # fallback：先检查否定词，避免 "不合理"/"不通过"/"未通过" 被正向词匹配误判
    if re.search(r'不通过|未通过|不合理|不正确|不符合|错误|失败|不合格', raw):
        passed = 0
    elif re.search(r'通过|合理|正确|"passed"\s*:\s*1|合格|可接受|不需要调用', raw):
        passed = 1
    else:
        passed = 0
    return {"passed": passed, "analysis": "", "reason": raw[:200]}


# ── 核心评测逻辑 ──────────────────────────────────────────────────────────────

def parse_evaluator_output(raw: str, evaluator: dict) -> dict:
    """Declared output contracts reject malformed/ambiguous model answers."""
    if not raw or not raw.strip():
        raise ValueError("模型未返回有效内容")
    contract = evaluator.get("output_contract")
    if contract not in OUTPUT_CONTRACTS:
        return parse_llm_output(raw)
    obj = json.loads(raw)
    field, domain = OUTPUT_CONTRACTS[contract]
    if not isinstance(obj, dict) or type(obj.get(field)) is not int or obj[field] not in domain:
        raise ValueError(f"返回不符合 {contract}：需要 {field} 属于 {domain}")
    if not isinstance(obj.get("reason"), str) or not obj["reason"].strip():
        raise ValueError("评测结果缺少 reason")
    if contract == "tristate_v1":
        return {"passed": int(obj[field] == 1), "outcome": obj[field], "reason": obj["reason"],
                "analysis": obj.get("analysis", "")}
    return {"passed": obj[field], "reason": obj["reason"], "analysis": obj.get("analysis", "")}


_MAX_RETRIES = 5
_RETRY_DELAY = 2.0  # seconds; 指数退避基数


async def _call_with_retry(prompt: str, client: httpx.AsyncClient) -> str:
    """Retries and per-request usage are owned by llm.client (one retry maximum)."""
    return await call_llm(prompt, client)


async def eval_step(
    step: dict,
    template: str,
    context_window: int,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    dry_run: bool,
) -> dict:
    prompt = render_prompt(template, step, context_window)
    if dry_run:
        result = {"passed": -1, "outcome": None, "reason": "[dry-run]"}
    else:
        async with sem:
            try:
                raw = await _call_with_retry(prompt, client)
                result = parse_llm_output(raw)
            except Exception as e:
                if getattr(e, "stop_reason", None):
                    raise
                result = {"passed": -1, "outcome": None, "analysis": "", "reason": f"ERROR:{type(e).__name__}: {e}"}
    return {
        "step":       step["step_index"],
        "agent_name": step["agent_name"],
        "passed":     result["passed"],
        "outcome":    result.get("outcome"),
        "analysis":   result.get("analysis", ""),
        "reason":     result["reason"],
    }


def _eval_inline_rules_only(trace: dict, evaluator: dict) -> dict:
    """纯规则评测（无 LLM 调用），适用于只有 inline_rules 没有 prompt 的评估器。"""
    if not _get_messages(trace):
        return None
    try:
        rules, threshold = load_rules_from_dict(evaluator["inline_rules"])
        from human_value._engine.core.features import extract as _feat
        feat = _feat(trace)
        r = apply_rules(feat, trace=trace, rules=rules, threshold=threshold)
    except Exception as e:
        r = {"score": 0, "passed": False, "issues": [f"inline_rules 执行错误: {e}"], "issue_detail": {},
             "rule_check": None, "rule_errors": {"_evaluation": f"{type(e).__name__}: {e}"}}
    passed_flag = bool(r.get("passed", False))
    issues = r.get("issues", [])
    reason = "、".join(issues) if issues else "全部通过"
    return {
        "trace_id":     trace.get("id", "")[:12],
        "timestamp":    str(trace.get("timestamp", ""))[:19],
        "langfuse_url": _get_langfuse_url(trace),
        "evaluator":    evaluator["name"],
        "iterate_over": "trace",
        "n_steps":      1,
        "n_passed":     1 if passed_flag else 0,
        "passed":       passed_flag,
        "score":        r.get("score", 0),
        "steps": [{"step": 1, "agent_name": "trace", "passed": 1 if passed_flag else 0, "reason": reason}],
        "rule_check":  r.get("rule_check"),
        "llm_check":   None,
        "rule_errors": r.get("rule_errors"),
        "rule_outcomes": r.get("rule_outcomes"),
        "execution_status": "technical_error" if r.get("rule_errors") else "ok",
    }


async def eval_trace(
    trace: dict,
    evaluator: dict,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    dry_run: bool,
    rule_ctx: dict = None,
) -> dict:
    from human_value._engine.core.evaluator_spec import evaluator_id, content_hash
    options = {}
    if evaluator.get("system_prompt"):
        options["system_prompt"] = evaluator["system_prompt"]
    if evaluator.get("output_contract") in OUTPUT_CONTRACTS:
        options["response_format"] = {"type": "json_object"}
    with model_runtime(options=options, trace_id=str(trace.get("id", "")),
                       evaluator_id=evaluator_id(evaluator)):
        if has_inline_rules(evaluator) and "prompt" not in evaluator:
            result = _eval_inline_rules_only(trace, evaluator)
        elif evaluator.get("iterate_over", "trace") == "trace":
            result = await _eval_trace_level(trace, evaluator, evaluator["prompt"], client, sem, dry_run, rule_ctx=rule_ctx)
        else:
            result = await _eval_dispatch_level(trace, evaluator, evaluator["prompt"], client, sem, dry_run)
    if result is not None:
        result.update(full_trace_id=str(trace.get("id", "")), evaluator_id=evaluator_id(evaluator),
                      evaluator_version=evaluator.get("evaluator_version") or evaluator.get("version"),
                      evaluator_hash=evaluator.get("evaluator_hash") or content_hash(evaluator))
    return result


async def _eval_trace_level(
    trace: dict,
    evaluator: dict,
    template: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    dry_run: bool,
    rule_ctx: dict = None,
) -> dict:
    msgs = _get_messages(trace)
    if not msgs:
        return None

    # inline_rules 优先级高于外部 rule_ctx：evaluator 自带规则时始终用自己的
    effective_rule_ctx = rule_ctx
    if has_inline_rules(evaluator):
        try:
            inline_cfg = evaluator["inline_rules"]
            rules, threshold = load_rules_from_dict(inline_cfg)
            from human_value._engine.core.features import extract as _feat
            feat = _feat(trace)
            effective_rule_ctx = apply_rules(feat, trace=trace, rules=rules, threshold=threshold)
        except Exception as e:
            effective_rule_ctx = {"score": "N/A", "passed": None,
                                  "issues": [f"inline_rules 执行错误: {e}"], "issue_detail": {}}

    ctx    = extract_trace_context(trace, rule_ctx=effective_rule_ctx)

    # fewshot 注入（仅当 evaluator 声明 enable_fewshot: true）
    if evaluator.get("enable_fewshot"):
        ctx["fewshot"] = _inject_fewshot(ctx, evaluator)
    else:
        ctx.setdefault("fewshot", "")

    prompt = render_trace_prompt(template, ctx)
    missing = sorted(set(evaluator.get("required_evidence", [])) - set(ctx["_available_evidence"]))
    if missing:
        return {"trace_id": str(trace.get("id", ""))[:12], "evaluator": evaluator["name"],
                "iterate_over": "trace", "n_steps": 1, "n_passed": 0, "passed": False,
                "outcome": None, "execution_status": "missing_evidence", "missing_evidence": missing,
                "steps": [{"step": 1, "agent_name": "trace", "passed": -1, "outcome": None,
                           "reason": "缺少必要证据：" + ", ".join(missing)}],
                "rule_check": None, "llm_check": None, "stability": None}

    if dry_run:
        result = {"passed": -1, "outcome": None, "analysis": "", "reason": "[dry-run]"}
        stability = None
    else:
        n_runs = int(evaluator.get("n_runs") or get_n_runs())
        run_results = []
        for run_i in range(n_runs):
            async with sem:
                try:
                    with model_runtime(repeat_index=run_i + 1):
                        raw = await _call_with_retry(prompt, client)
                    run_results.append(parse_evaluator_output(raw, evaluator))
                except Exception as e:
                    if getattr(e, "stop_reason", None):
                        raise
                    run_results.append({"passed": -1, "outcome": None, "analysis": "", "reason": f"ERROR:{type(e).__name__}: {e}"})

        from collections import Counter as _Counter
        # 三态投票：优先在 outcome(1/0/-1) 上投票——取出现最多的；平票取「最早出现」的那个
        # （= 第一次的结果）。无 outcome 的老式二值评估器回退到 passed(0/1) 多数票。
        valid_tri = [r for r in run_results if r.get("outcome") in (1, 0, -1)]
        if valid_tri:
            counts = _Counter(r["outcome"] for r in valid_tri)
            top = max(counts.values())
            tied = {o for o, c in counts.items() if c == top}          # 并列最多的 outcome 集合
            win = next(r["outcome"] for r in valid_tri if r["outcome"] in tied)  # 最早出现者
            rep = next(r for r in valid_tri if r["outcome"] == win)
            result = {"passed": 1 if win == 1 else 0, "outcome": win,
                      "analysis": rep.get("analysis", ""), "reason": rep.get("reason", "")}
            vote_vals = [r["outcome"] for r in valid_tri]
        else:
            valid_b = [r for r in run_results if r["passed"] in (0, 1)]
            if valid_b:
                n_pass = sum(r["passed"] for r in valid_b)
                majority = 1 if n_pass * 2 > len(valid_b) else 0
                rep = next((r for r in valid_b if r["passed"] == majority), valid_b[0])
                result = {"passed": majority, "outcome": rep.get("outcome"),
                          "analysis": rep.get("analysis", ""), "reason": rep.get("reason", "")}
                vote_vals = [r["passed"] for r in valid_b]
            else:
                result = {"passed": -1, "outcome": None, "analysis": "",
                          "reason": run_results[0]["reason"] if run_results else "no runs"}
                vote_vals = []

        if n_runs > 1:
            n_agree = max(_Counter(vote_vals).values()) if vote_vals else 0
            stability = {
                "n_runs":         n_runs,
                "run_results":    [
                    {"run": i + 1, "passed": r["passed"], "outcome": r.get("outcome"),
                     "label": r.get("label", ""), "analysis": r.get("analysis", ""),
                     "reason": r.get("reason", "")[:300]}
                    for i, r in enumerate(run_results)
                ],
                "is_stable":      len(vote_vals) == n_runs and len(set(vote_vals)) == 1,
                "n_agree":        n_agree,
                "agreement_rate": round(n_agree / n_runs, 2) if n_runs else 0.0,
                "valid_runs": len(vote_vals), "technical_errors": n_runs - len(vote_vals),
            }
        else:
            stability = None

    passed = result["passed"]
    llm_weight    = float(evaluator.get("llm_weight", 100))
    llm_threshold = int(evaluator.get("llm_threshold", 60))
    llm_score     = max(0.0, 100.0 - llm_weight) if passed == 0 else 100.0
    llm_passed_flag = llm_score >= llm_threshold
    return {
        "trace_id":    trace.get("id", "")[:12],
        "timestamp":   str(trace.get("timestamp", ""))[:19],
        "langfuse_url": _get_langfuse_url(trace),
        "evaluator":   evaluator["name"],
        "iterate_over": "trace",
        "n_steps":     1,
        "n_passed":    max(0, passed),
        "passed":      passed == 1,
        "outcome":     result.get("outcome"),
        "stability":   stability,
        "execution_status": ("dry_run" if dry_run else "technical_error" if passed == -1
                             else "partial_error" if stability and stability.get("technical_errors") else "ok"),
        "steps": [{"step": 1, "agent_name": "trace", "passed": passed, "outcome": result.get("outcome"), "analysis": result.get("analysis", ""), "reason": result["reason"]}],
        "rule_check": effective_rule_ctx.get("rule_check") if effective_rule_ctx else None,
        "llm_check": {
            "score":     llm_score,
            "threshold": llm_threshold,
            "passed":    llm_passed_flag,
            "judges": [{
                "name":        evaluator.get("llm_judge_name", "default"),
                "description": evaluator.get("description", ""),
                "weight":      llm_weight,
                "severity":    "critical",
                "passed":      passed if passed in (0, 1) else None,
                "reason":      result.get("reason", ""),
                "analysis":    result.get("analysis", ""),
            }],
            "triggered_judges": [] if llm_passed_flag else [{
                "name":     evaluator.get("llm_judge_name", "default"),
                "weight":   llm_weight,
                "severity": "critical",
                "reason":   result.get("reason", ""),
            }],
        } if passed != -1 else None,
    }


async def _eval_dispatch_level(
    trace: dict,
    evaluator: dict,
    template: str,
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    dry_run: bool,
) -> dict:
    context_window = evaluator.get("context_window", 6)
    steps = extract_dispatch_steps(trace, context_window)
    if not steps:
        return None

    tasks        = [eval_step(s, template, context_window, client, sem, dry_run) for s in steps]
    step_results = await asyncio.gather(*tasks)

    valid    = [s for s in step_results if s["passed"] != -1]
    n_passed = sum(s["passed"] for s in valid)
    n_steps  = len(valid)

    # 逐 step 的 outcome 聚合成 trace 级三态：任一 step FAIL(-1) 就整体 FAIL；
    # 没有任何 step 带 outcome（旧式 prompt，parse_llm_output 不产出这个字段）则整体 None。
    step_outcomes = [s.get("outcome") for s in step_results if s.get("outcome") is not None]
    if not step_outcomes:
        outcome = None
    elif -1 in step_outcomes:
        outcome = -1
    elif 1 in step_outcomes:
        outcome = 1
    else:
        outcome = 0

    return {
        "trace_id":    trace.get("id", "")[:12],
        "timestamp":   str(trace.get("timestamp", ""))[:19],
        "langfuse_url": _get_langfuse_url(trace),
        "evaluator":   evaluator["name"],
        "iterate_over": "dispatch_steps",
        "n_steps":     n_steps,
        "n_passed":    n_passed,
        "passed":      n_passed == n_steps and n_steps > 0,
        "outcome":     outcome,
        "steps":       list(step_results),
        "rule_check": None,
        "llm_check":  {"steps": list(step_results)},
    }


async def run_all_evaluators(
    traces: list,
    evaluators: list,
    dry_run: bool = False,
    on_progress=None,
    rule_by_id: dict = None,
    concurrency: int = None,
    n_runs: int = None,
) -> tuple[list, dict]:
    """
    对多个评估器依次运行 run_async()，返回 (all_results, stats_by_evaluator)。

    on_progress(ev_name) -> callback(current, total) —— 可选工厂，返回单个评估器的进度回调
    rule_by_id: {trace_id[:12]: rule_result_dict}，由 pipeline 传入供 prompt 注入
    """
    validate_rule_deps(evaluators, has_rules=bool(rule_by_id))
    all_results: list = []
    stats: dict = {}
    for ev in evaluators:
        ev_name = ev.get("name", "?")
        if n_runs:                              # per-request 覆盖「一个 case 跑几次」，不改原 dict
            ev = {**ev, "n_runs": int(n_runs)}
        cb = on_progress(ev_name) if on_progress else None
        try:
            ev_results = await run_async(traces, ev, dry_run=dry_run, on_progress=cb,
                                         rule_by_id=rule_by_id, concurrency=concurrency)
            all_results.extend(ev_results)
            n_pass = sum(1 for r in ev_results if r["passed"])
            stats[ev_name] = {
                "total":     len(ev_results),
                "passed":    n_pass,
                "pass_rate": round(n_pass / len(ev_results) * 100, 1) if ev_results else 0,
            }
        except Exception as e:
            if getattr(e, "stop_reason", None):
                raise
            # ⚠ 2026-07-29：此处曾静默吞异常，整批返回 0 条且前端无任何提示。必须打堆栈。
            import sys as _sys, traceback as _tb
            print(f"[llm.eval] 评估器「{ev_name}」整体失败，该评估器 0 条结果: "
                  f"{type(e).__name__}: {e}", file=_sys.stderr)
            _tb.print_exc(file=_sys.stderr)
            stats[ev_name] = {"total": 0, "passed": 0, "pass_rate": 0, "error": f"{type(e).__name__}: {e}"}
    return all_results, stats


async def run_async(
    traces: list, evaluator: dict, dry_run: bool, on_progress=None,
    rule_by_id: dict = None, concurrency: int = None,
) -> list:
    sem = asyncio.Semaphore(min(20, max(1, int(concurrency) if concurrency else get_concurrency())))
    results = []
    from human_value._engine.llm.client import get_http_trust_env
    async with httpx.AsyncClient(trust_env=get_http_trust_env()) as client:
        tasks = [asyncio.create_task(eval_trace(
                t, evaluator, client, sem, dry_run,
                rule_ctx=(rule_by_id or {}).get(str(t.get("id", ""))) or (rule_by_id or {}).get(str(t.get("id", ""))[:12]),
            )) for t in traces]
        try:
            for i, coro in enumerate(asyncio.as_completed(tasks), 1):
                r = await coro
                if r:
                    results.append(r)
                if on_progress:
                    on_progress(i, len(tasks))
                elif i % 20 == 0 or i == len(tasks):
                    print(f"  进度: {i}/{len(tasks)}")
        finally:
            # Drain in-flight calls. Queued calls check the same stop/budget guard
            # before sending; no background tasks escape the evaluation run.
            await asyncio.gather(*tasks, return_exceptions=True)

    return results


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LLM 评测")
    parser.add_argument("--data",    required=True, help="normalized trace JSON 路径")
    parser.add_argument("--out",     default="./output", help="输出根目录，实际写入 <out>/<YYYYMMDD_HHMM>[_sampleN]/（默认 ./output）")
    parser.add_argument("--dry-run", action="store_true", help="不调用 LLM，只走流程")
    parser.add_argument("--sample",  type=int, default=None, help="只跑前 N 条 trace（用于测试）")
    args = parser.parse_args()

    evals_cfg = yaml.safe_load(open(_EVALS_PATH, encoding="utf-8"))
    evaluators = [e for e in evals_cfg.get("evaluators", []) if e.get("enabled", True)]
    if not evaluators:
        print("没有启用的评估器，退出。")
        return

    with open(args.data, encoding="utf-8") as f:
        traces = json.load(f)
    if args.sample:
        traces = traces[:args.sample]
    print(f"加载 {len(traces)} 条 trace，启用评估器: {[e['name'] for e in evaluators]}")

    all_results = []
    for ev in evaluators:
        print(f"\n▶ 评估器: {ev['name']}  ({'dry-run' if args.dry_run else '实际调用'})")
        results = asyncio.run(run_async(traces, ev, dry_run=args.dry_run))
        all_results.extend(results)
        passed = sum(1 for r in results if r["passed"])
        print(f"  完成: {len(results)} 条，通过 {passed}/{len(results)}")

    tag     = f"_sample{args.sample}" if args.sample else ""
    ts      = datetime.now().strftime("%Y%m%d_%H%M") + tag
    out_dir = Path(args.out) / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "llm_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    meta = write_meta(out_dir, Path(args.data), stage="llm", n_traces=len(traces))
    print(f"\n结果 → {out_file}")
    print(f"数据源 hash → {meta['source_hash']}  ({meta['source_file']})")


if __name__ == "__main__":
    main()
