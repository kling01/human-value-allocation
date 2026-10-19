"""
规则注册表。

规则的全部定义（名称/权重/严重级别/开关/check 表达式）均来自 config/rules.yaml。
本文件不需要随规则变化而修改。

添加规则：只改 config/rules.yaml，加一项即可。
改权重/开关：只改 config/rules.yaml。

check 表达式语法：
  可用字段（对应 TraceFeatures）：
    n_dispatch, has_final_output, final_content,
    n_env_fail, n_real_fail, n_empty_return, n_success_return,
    agent_names, is_env_contaminated
  可用模块：re（正则）
  示例：
    "n_dispatch > 1"
    "re.search(r'执行失败|错误', final_content) is not None"
    "'example_agent' in agent_names and n_dispatch > 3"

业务特定规则：在 adapters/xxx_rules.py 里构造 Rule 对象，
通过 apply_rules(f, extra_rules=[...]) 传入，不要写进 rules.yaml。
"""

import dataclasses
import re as _re
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from human_value._engine.core.features import TraceFeatures
from human_value._engine.core.feature_registry import build_eval_namespace, is_rule_activated, register_tool_calls

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "common" / "rules.yaml"

# eval 可用的字段名：从 TraceFeatures 的 dataclass 字段自动派生，
# 不再手写维护——加字段只需改 core/features.py 一处（见 feature_registry.py）。
# 排除：trace_id/timestamp（元数据，非判断信号）、skipped/skip_reason（跳过标记，非判断信号）。
_EXCLUDED_FEATURE_FIELDS = frozenset({"trace_id", "timestamp", "skipped", "skip_reason"})
_FEATURE_FIELDS = [
    f.name for f in dataclasses.fields(TraceFeatures)
    if f.name not in _EXCLUDED_FEATURE_FIELDS
]

# 公开接口：供外部工具（如 Socrates stage_gaps）查询已知特征集合，
# 避免对方用正则解析本文件源码。
FEATURE_FIELDS_PUBLIC: frozenset[str] = frozenset(_FEATURE_FIELDS)


def _has_dead_loop(agent_names: list, n: int = 3) -> bool:
    """连续 n 次调用同一 agent 判定为死循环。"""
    for i in range(len(agent_names) - n + 1):
        if len(set(agent_names[i:i + n])) == 1:
            return True
    return False


def make_check(expr: str) -> Callable[[TraceFeatures, Optional[dict]], bool]:
    """把 YAML 里的 check 表达式字符串编译成 check 函数。

    check(f)     旧调用方式：只用 TraceFeatures 标量字段（向后兼容，行为字节级不变）。
    check(f, ns) 新调用方式：ns 是 apply_rules() 传 trace 时用 build_eval_namespace()
                 构造好的完整命名空间（scalar+calls+helper+root），一条 trace 只建一次，
                 所有规则共用，不在这里重复计算。
    """
    def check(f: TraceFeatures, ns: Optional[dict] = None) -> bool:
        if ns is None:
            ns = {field: getattr(f, field) for field in _FEATURE_FIELDS}
            ns["re"] = _re
            ns["len"] = len
            ns["set"] = set
            ns["any"] = any
            ns["all"] = all
            ns["range"] = range
            ns["min"] = min
            ns["max"] = max
            ns["sum"] = sum
            ns["dead_loop"] = _has_dead_loop
            ns["__builtins__"] = {}
        return bool(eval(expr, ns))  # noqa: S307
    check.__doc__ = expr
    return check


@dataclass
class Rule:
    id:          str
    name:        str
    description: str
    check_expr:  str
    weight:      float
    severity:    str
    enabled:     bool
    check:       Callable[[TraceFeatures, Optional[dict]], bool]
    scope:       str = "trace"  # trace(默认)|dispatch|tool:X|agent:X，见 is_rule_activated()


def _load_rules(config_path: Path = _CONFIG_PATH) -> tuple:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return load_rules_from_dict(cfg)


def load_rules_from_dict(cfg: dict) -> tuple:
    """从已解析的 dict 加载规则（供 inline_rules 使用）。

    同时支持两种格式：
      - 直接格式：顶层有 rules/items 列表
      - evaluators 格式：顶层有 evaluators，规则藏在各 evaluator 的 inline_rules 里
        （只提取没有 prompt 的纯代码规则评估器）
    """
    # evaluators 格式兼容：把所有纯规则评估器的 inline_rules 合并成一个规则列表
    if "evaluators" in cfg and "rules" not in cfg and "items" not in cfg:
        merged: list = []
        threshold_val = 60
        for ev in cfg.get("evaluators", []):
            ir = ev.get("inline_rules")
            if ir and not ev.get("prompt"):
                threshold_val = int(ir.get("threshold", threshold_val))
                merged.extend(ir.get("rules", ir.get("items", [])))
        cfg = {"pass_threshold": threshold_val, "rules": merged}

    threshold = int(cfg.get("pass_threshold", cfg.get("threshold", 60)))
    rules = []
    for item in cfg.get("rules", cfg.get("items", [])):
        name = item["name"]
        rule_id = item.get("id", name)
        expr = item.get("check", "")
        if not expr:
            raise ValueError(f"规则 {name!r} 缺少 check 字段")
        rules.append(Rule(
            id=rule_id,
            name=name,
            description=item.get("description", ""),
            check_expr=expr,
            weight=float(item.get("weight", 0)),
            severity=item.get("severity", "warning"),
            enabled=bool(item.get("enabled", True)),
            check=make_check(expr),
            scope=item.get("scope", "trace"),
        ))

    # scope: "tool:X" 的规则需要 X_calls 特征可用——一次性全部注册，
    # 不用每条规则各自记得调 register_tool_calls()。
    for r in rules:
        if r.scope.startswith("tool:"):
            register_tool_calls(r.scope.split(":", 1)[1])

    return rules, threshold


RULES, PASS_THRESHOLD = _load_rules()


def apply_rules(
    f: TraceFeatures,
    trace: Optional[dict] = None,
    extra_rules: Optional[List[Rule]] = None,
    rules: Optional[List[Rule]] = None,
    threshold: Optional[int] = None,
) -> dict:
    """
    对一条 trace 的 features 跑所有规则，返回结果 dict。

    extra_rules: 业务适配器传入的额外规则，追加在通用规则之后。
    rules:       完全覆盖默认 RULES（一般不用）。
    threshold:   覆盖默认 PASS_THRESHOLD（一般不用）。
    trace:       原始 cleaned trace（可选）。传了才会：
                   1. 用 build_eval_namespace() 给规则接上 root/calls/helper（scope != trace 的规则需要）
                   2. 按 scope 做激活探针，未激活的规则 outcome=0，不参与 triggered/扣分
                 不传时行为与旧版 apply_rules(f, ...) 完全一致（所有规则视为 scope=trace 恒激活）。

    每条规则的三态结果（1=PASS/0=未激活/-1=FAIL）写进返回值的 rule_outcomes 和
    rule_check.all_rules[].outcome，字段是新增的，不影响 score/passed/issues 的既有计算方式。
    """
    active_rules = (rules if rules is not None else RULES) + (extra_rules or [])
    active_threshold = threshold if threshold is not None else PASS_THRESHOLD

    ns = None
    root: dict = {}
    if trace is not None:
        root = trace.get("root") or {}
        ns = build_eval_namespace(trace, features=f)
        ns["dead_loop"] = _has_dead_loop

    outcomes: dict = {}
    rule_errors: dict = {}
    for r in active_rules:
        if not r.enabled:
            continue
        if trace is not None and not is_rule_activated(r.scope, root):
            outcomes[r.id] = 0
            continue
        try:
            outcomes[r.id] = -1 if r.check(f, ns) else 1
        except Exception as exc:
            # 单条规则表达式报错不拖垮整批评测：outcome=None（无有效信号），
            # 不计入 triggered/扣分，错误信息记录在 rule_errors 供排查。
            outcomes[r.id] = None
            rule_errors[r.id] = f"{type(exc).__name__}: {exc}"

    triggered = [r for r in active_rules if r.enabled and outcomes.get(r.id) == -1]
    score = max(0.0, 100.0 - sum(r.weight for r in triggered))

    return {
        "trace_id":        f.trace_id,
        "timestamp":       f.timestamp,
        "score":           score,
        "passed":          score >= active_threshold,
        "pass_threshold":  active_threshold,
        "issues":          [r.id for r in triggered],
        "issue_detail":    {r.id: r.description for r in triggered},
        "is_contaminated": f.is_env_contaminated,
        "n_dispatch":      f.n_dispatch,
        "agent_names":     f.agent_names,
        "rule_outcomes":   outcomes,
        "rule_errors":     rule_errors,
        "rule_check": {
            "threshold": active_threshold,
            "score":     score,
            "passed":    score >= active_threshold,
            "all_rules": [
                {
                    "id":           r.id,
                    "name":         r.name,
                    "description":  r.description,
                    "check_expr":   r.check_expr,
                    "weight":       r.weight,
                    "severity":     r.severity,
                    "passed":       r not in triggered,
                    "outcome":      outcomes.get(r.id),
                    "check_detail": f"{r.check_expr} = {'false' if r in triggered else 'true'}",
                }
                for r in active_rules if r.enabled
            ],
            "triggered_rules": [
                {
                    "id":           r.id,
                    "name":         r.name,
                    "description":  r.description,
                    "check_expr":   r.check_expr,
                    "weight":       r.weight,
                    "severity":     r.severity,
                    "outcome":      outcomes.get(r.id),
                    "check_detail": f"{r.check_expr} = false",
                }
                for r in triggered
            ],
        },
    }
