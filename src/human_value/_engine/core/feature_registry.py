"""
特征注册表 —— agent-eval-v2 与 Socrates 共用的 eval() 命名空间构造器。

三层特征，统一注册、统一发现：
  scalar  trace 级聚合标量。不重新实现——直接内省 TraceFeatures 的 dataclass 字段。
          62 年来的规则引擎（core/rules.py）只用这一层，本文件不改变它的任何行为。
  calls   某个工具每一次调用的结构化记录（input/output），供逐调用参数校验规则使用。
          任意工具名一行 register_tool_calls() 即可，不需要为每个工具手写提取代码。
  helper  原始 trace 访问函数 + 常用 builtins，escape hatch，供 calls/scalar 都覆盖不到的
          规则直接操作 root 时使用。

用法：
    from human_value._engine.core.feature_registry import build_eval_namespace
    ns = build_eval_namespace(cleaned_trace_dict)
    result = eval(check_expr, ns)

扩展方式：
  - 新增 calls 特征：register_tool_calls("some_tool_name") 即可，无需额外代码。
  - 新增 helper：用 @register(name, kind="helper") 包一个函数。
  - 新增 scalar：照常在 core/features.py 的 TraceFeatures 加字段（用 _f() 带上 category），
                 这里自动纳入，无需改本文件。
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Optional

from human_value._engine.core.features import TraceFeatures, extract as _extract_features
from human_value._engine.core.trace_nav import (
    get_all_tool_steps,
    get_conversation_messages,
    get_final_ai_message,
    get_tool_steps,
    safe_loads,
    tool_was_called,
)

import json as _json
import re as _re
import datetime as _datetime


@dataclasses.dataclass
class FeatureSpec:
    name: str
    kind: str          # "scalar" | "calls" | "helper"
    category: str
    doc: str
    fn: Optional[Callable] = None  # scalar 无 fn（值直接来自 TraceFeatures 实例）


_REGISTRY: dict[str, FeatureSpec] = {}


def register(name: str, kind: str = "calls", category: str = "misc", doc: str = ""):
    """装饰器：把函数注册为 calls 或 helper 特征。scalar 走 _adopt_scalar_fields()，不用这个。"""
    def deco(fn: Callable) -> Callable:
        _REGISTRY[name] = FeatureSpec(name=name, kind=kind, category=category,
                                       doc=doc or (fn.__doc__ or "").strip(), fn=fn)
        return fn
    return deco


def register_tool_calls(tool_name: str, category: str = "tool_calls") -> None:
    """
    通用 calls 特征工厂：注册 "{tool_name}_calls"，值为该工具每次调用的记录列表：
        [{"input": dict, "output_raw": str|None, "output": dict, "_step": node}, ...]
    input 已是解析好的 dict（cleaned trace 契约保证）；output 原样保留字符串于 output_raw，
    并额外提供 safe_loads() 解析后的 dict 到 output，规则表达式两种都能用。
    """
    feature_name = f"{tool_name}_calls"

    def _calls(root: dict, _tool: str = tool_name) -> list:
        return [
            {
                "input": s.get("input") or {},
                "output_raw": s.get("output"),
                "output": safe_loads(s.get("output")),
                "_step": s,
            }
            for s in get_tool_steps(root, _tool)
        ]

    _REGISTRY[feature_name] = FeatureSpec(
        name=feature_name, kind="calls", category=category,
        doc=f"{tool_name} 每次调用的 (input, output) 记录列表", fn=_calls,
    )


def _adopt_scalar_fields() -> None:
    """把 TraceFeatures 的 dataclass 字段全部注册为 scalar 特征（幂等，模块加载时跑一次）。"""
    for f in dataclasses.fields(TraceFeatures):
        meta = f.metadata or {}
        _REGISTRY[f.name] = FeatureSpec(
            name=f.name, kind="scalar",
            category=meta.get("category", "misc"),
            doc=meta.get("doc", ""), fn=None,
        )


def _register_helpers() -> None:
    """原始 trace 访问函数 + 常用 builtins，统一注册为 helper 层，便于 manifest 展示。"""
    helpers = {
        "get_tool_steps": get_tool_steps,
        "get_all_tool_steps": get_all_tool_steps,
        "get_final_ai_message": get_final_ai_message,
        "get_conversation_messages": get_conversation_messages,
        "tool_was_called": tool_was_called,
        "safe_loads": safe_loads,
    }
    for name, fn in helpers.items():
        _REGISTRY[name] = FeatureSpec(
            name=name, kind="helper", category="trace_nav",
            doc=(fn.__doc__ or "").strip(), fn=fn,
        )


_adopt_scalar_fields()
_register_helpers()


# ── builtins 白名单：eval() 沙箱里能用的原生函数/模块，不进注册表（不是"特征"） ──
_SAFE_BUILTINS = {
    "json": _json, "re": _re,
    # datetime：日期/时间比较（lead-time/deadline/住期/renewal-period 等跨域时序约束需要）。
    # 2026-07-08 补：hotel/airline/retail/dmv 的日期规则依赖它，同 next/map/abs 属白名单缺项补齐。
    "datetime": _datetime, "date": _datetime.date, "timedelta": _datetime.timedelta,
    "timezone": _datetime.timezone,
    "len": len, "any": any, "all": all, "set": set, "list": list, "dict": dict,
    "str": str, "int": int, "bool": bool, "float": float, "type": type, "range": range,
    "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
    "map": map, "filter": filter, "zip": zip, "enumerate": enumerate,
    "sorted": sorted, "reversed": reversed, "tuple": tuple, "next": next,
    "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
}


def build_eval_namespace(trace: dict, features: Optional[TraceFeatures] = None) -> dict[str, Any]:
    """
    构造统一 eval() 命名空间：scalar(TraceFeatures 全部字段)
                              + calls(所有已注册工具的调用记录)
                              + helper(原始访问函数) + builtins。

    Socrates 的 assign_methods.py 与未来任何需要同时访问标量特征 + 原始 trace
    的消费方都应该走这一个函数，不要各自手搭命名空间。

    features: 调用方若已经跑过 extract(trace)，传进来复用，避免重复提取
              （core/rules.py 的 apply_rules() 就是这样用的——一条 trace 只提取一次，
              N 条规则共用同一份命名空间）。不传则内部自己算一次。
    """
    root = trace.get("root") or {}

    features = features if features is not None else _extract_features(trace)
    ns: dict[str, Any] = {
        spec.name: getattr(features, spec.name)
        for spec in _REGISTRY.values() if spec.kind == "scalar"
    }

    for spec in _REGISTRY.values():
        if spec.kind == "calls":
            ns[spec.name] = spec.fn(root)
        elif spec.kind == "helper":
            ns[spec.name] = spec.fn

    ns["root"] = root
    ns["input"] = root.get("input") or {}
    ns["output"] = root.get("output") or {}
    ns["trace"] = trace  # 顶层元数据（id/thread_id/timestamp/dispatch_tool_name，见协议§1）

    ns.update(_SAFE_BUILTINS)
    ns["__builtins__"] = {}
    return ns


def is_rule_activated(scope: str, root: dict) -> bool:
    """
    规则激活探针：判断某条规则对本条 trace 是否"该被评"（结构性判断，不涉及语义）。
    code 规则的三态输出直接靠这个决定 0（未激活）；LLM 规则不靠它计数（见 docs/tristate_rules.md）。

    scope 取值：
      trace       一直激活（默认）
      dispatch    只要调用过 dispatch_subagent
      tool:X      只要调用过工具 X
      agent:X     只要 dispatch_subagent 派发过 agent X
    """
    scope = scope or "trace"
    if scope == "trace":
        return True
    if scope == "dispatch":
        return bool(get_tool_steps(root, "dispatch_subagent"))
    if scope.startswith("tool:"):
        return bool(get_tool_steps(root, scope.split(":", 1)[1]))
    if scope.startswith("agent:"):
        agent = scope.split(":", 1)[1]
        return any((s.get("input") or {}).get("agent_name") == agent
                   for s in get_tool_steps(root, "dispatch_subagent"))
    return True


def feature_manifest() -> list[dict]:
    """特征清单，供 scripts/dump_feature_manifest.py 生成 docs/FEATURES.md。"""
    return sorted(
        (
            {"name": s.name, "kind": s.kind, "category": s.category, "doc": s.doc}
            for s in _REGISTRY.values()
        ),
        key=lambda d: (d["kind"], d["category"], d["name"]),
    )
