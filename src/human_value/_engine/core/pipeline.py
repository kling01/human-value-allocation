"""
core/pipeline.py —— 评测编排层

将规则评测 + LLM 评测 + 报告合并的全流程封装为一个可复用的函数，
server.py 只负责 HTTP 解析，CLI 脚本可直接调用此模块。

公开接口：
  PipelineConfig   —— 运行参数
  PipelineResult   —— 运行结果
  run_combined()   —— 异步主入口
"""

from __future__ import annotations

import logging
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from human_value._engine.core.rules import _load_rules
from human_value._engine.core.evaluator_spec import normalize_config
from human_value._engine.llm.runtime import model_runtime, public_model_config
from human_value._engine.evaluate import run as _run_rules, make_report as _make_rules_report
from human_value._engine.llm.eval import run_all_evaluators, validate_rule_deps, has_inline_rules
from human_value._engine.report import render_report

log = logging.getLogger("eval.pipeline")

_ROOT            = Path(__file__).parent.parent
_RULES_PATH      = _ROOT / "config" / "common" / "rules.yaml"
_LLM_EVALS_PATH  = _ROOT / "config" / "common" / "llm_evals.yaml"
_LLM_CONFIG_PATH = _ROOT / "config" / "base" / "llm_config.yaml"


# ── 配置与结果 ─────────────────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    rules_yaml:  str  = ""      # 代码规则 YAML 文本（空则从默认文件读）
    llm_yaml:    str  = ""      # LLM 评估器 YAML 文本（空则跳过 LLM 评测）
    enable_llm:  bool = True    # 前端开关；False 时即使有 llm_yaml 也跳过
    dry_run:     bool = False   # 传给 LLM 评测，不实际调用大模型
    source_name: str  = ""      # 用于报告显示的数据源名称（如文件名）
    concurrency: int  = 0       # per-request 并发覆盖（0=用 config 默认）
    n_runs:      int  = 0       # per-request「一个 case 跑几次」覆盖（0=用 evaluator/config 默认）
    llm_options: dict = field(default_factory=dict, repr=False)
    usage_sink: object = field(default=None, repr=False)
    stage: str = "evaluation"
    version: str = "baseline"


@dataclass
class PipelineResult:
    rule_results:      list       # apply_rules() 每条 trace 的结果（兼容层）
    llm_results:       list       # LLM 评测每条结果（兼容层）
    llm_stats:         dict       # 每个 evaluator 的汇总统计
    n_skipped:         int        # 被 extract() 跳过的 trace 数
    report:            str        # 完整合并报告（Markdown）
    report_rules_only: str        # 纯规则报告（Markdown）
    stats:             dict = field(default_factory=dict)  # 聚合数字
    rules:             list = field(default_factory=list)  # 本次实际使用的规则
    evaluator_results: list = field(default_factory=list)  # 统一评估器结果
    model_config: dict = field(default_factory=dict)


# ── 内部工具 ───────────────────────────────────────────────────────────────────

def _load_llm_base_url() -> str:
    if not _LLM_CONFIG_PATH.exists():
        return ""
    cfg = yaml.safe_load(_LLM_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return str(cfg.get("base_url", "")).rstrip("/")


def _parse_llm_evaluators(llm_yaml: str) -> list:
    if not llm_yaml.strip():
        return []
    document = yaml.safe_load(llm_yaml)
    if not isinstance(document, dict):
        return []
    cfg = normalize_config(document)
    return [e for e in cfg.get("evaluators", []) if e.get("enabled", True)]


def _load_rules_from_yaml(rules_yaml: str):
    """从 YAML 文本加载规则，返回 (rules, threshold)。"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
        f.write(rules_yaml)
        tmp = Path(f.name)
    try:
        return _load_rules(tmp)
    finally:
        tmp.unlink(missing_ok=True)


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def run_combined(
    traces: list,
    config: PipelineConfig,
    on_llm_progress=None,
) -> PipelineResult:
    """
    完整评测流水线：
      1. 规则评测
      2. LLM 评测（可选）
      3. 报告合并

    on_llm_progress(evaluator_name, current, total) —— 可选进度回调
    """
    # ── 1. 加载规则 ────────────────────────────────────────────────────────────
    if config.rules_yaml.strip():
        rules, threshold = _load_rules_from_yaml(config.rules_yaml)
        log.info("pipeline: custom rules loaded  count=%d  threshold=%d", len(rules), threshold)
    else:
        rules, threshold = None, None
        log.info("pipeline: no rules_yaml provided, skipping standalone rule eval")

    # ── 2. 规则评测（仅当显式上传了 rules_yaml 时才跑）────────────────────────
    if rules is not None:
        rule_results, n_skipped = _run_rules(traces, rules=rules, threshold=threshold)
        log.info("pipeline: rule eval done  results=%d  skipped=%d", len(rule_results), n_skipped)
    else:
        rule_results, n_skipped = [], 0

    # rule_by_id 供 LLM prompt 注入 + 报告合并共用
    rule_by_id = {r.get("full_trace_id", r["trace_id"]): r for r in rule_results}

    # ── 3. LLM 评测 ───────────────────────────────────────────────────────────
    llm_results: list = []
    llm_stats:   dict = {}
    base_url = config.llm_options.get("base_url") or _load_llm_base_url()

    # 先解析评估器列表，以判断是否有纯规则评估器（不依赖 LLM base_url）
    evaluators_all = _parse_llm_evaluators(config.llm_yaml) if config.llm_yaml.strip() else []
    has_pure_rule_evals = any(has_inline_rules(ev) and not ev.get("prompt") for ev in evaluators_all)
    # 纯规则评估器不依赖 LLM 开关，始终执行；LLM 评估器需要 enable_llm=True 且 base_url 可用
    should_run_evals = bool(evaluators_all) and (
        has_pure_rule_evals
        or (config.enable_llm and bool(base_url))
    )
    # LLM 评估器是否实际调用：需要 enable_llm=True 且 base_url 可用
    llm_dry_run = config.dry_run or not config.enable_llm or not bool(base_url)

    if should_run_evals:
        evaluators = evaluators_all
        log.info("pipeline: eval start  evaluators=%d  traces=%d  llm_dry_run=%s",
                 len(evaluators), len(traces), llm_dry_run)

        # 校验评估器的规则字段依赖（规则结果存在则视为 has_rules）
        validate_rule_deps(evaluators, has_rules=bool(rule_results))

        def make_progress_cb(ev_name: str):
            outer_cb = on_llm_progress(ev_name) if on_llm_progress else None
            def cb(current: int, total: int):
                log.info("pipeline: llm progress  evaluator=%s  %d/%d", ev_name, current, total)
                if outer_cb:
                    outer_cb(current, total)
            return cb

        with model_runtime(options=config.llm_options, usage_sink=config.usage_sink,
                           stage=config.stage, version=config.version):
            llm_results, llm_stats = await run_all_evaluators(
                traces, evaluators, dry_run=llm_dry_run, on_progress=make_progress_cb,
                rule_by_id=rule_by_id,
                concurrency=(config.concurrency or None), n_runs=(config.n_runs or None),
            )
        log.info("pipeline: eval done  total_results=%d", len(llm_results))
    else:
        log.info("pipeline: skipping evals  enable_llm=%s  base_url=%s  llm_yaml=%s",
                 config.enable_llm, bool(base_url), bool(config.llm_yaml.strip()))

    # ── 4. 报告合并 ───────────────────────────────────────────────────────────
    llm_data  = None
    if llm_results:
        llm_map = defaultdict(dict)
        for r in llm_results:
            llm_map[r.get("full_trace_id", r["trace_id"])][r["evaluator"]] = r
        llm_data = dict(llm_map)

    report           = render_report({r.get("full_trace_id", r["trace_id"]): r for r in rule_results}, llm_data)
    meta             = {"path": config.source_name, "dates": "全量"}
    report_rules_only = _make_rules_report(rule_results, meta, n_skipped=n_skipped, rules=rules)

    # ── 5. 构建统一 evaluator_results ────────────────────────────────────────
    # 独立规则路径的结果转换为评估器格式
    rule_ev_results = []
    for r in rule_results:
        rule_ev_results.append({
            "trace_id":    r["trace_id"][:12],
            "full_trace_id": r.get("full_trace_id", r["trace_id"]),
            "timestamp":   r.get("timestamp", ""),
            "langfuse_url": r.get("langfuse_url", ""),
            "evaluator":   "__rules__",
            "iterate_over": "trace",
            "n_steps":     1,
            "n_passed":    1 if r["passed"] else 0,
            "passed":      r["passed"],
            "score":       r["score"],
            "steps": [{"step": 1, "agent_name": "trace",
                       "passed": 1 if r["passed"] else 0,
                       "reason": "、".join(r.get("issues", [])) or "全部通过"}],
            "rule_outcomes": r.get("rule_outcomes"),  # {rule_id: 1|0|-1}，逐条规则三态结果
            "rule_detail": r.get("rule_detail"),
            "rule_errors": r.get("rule_errors"),
            "llm_detail":  None,
        })
    evaluator_results = rule_ev_results + llm_results

    stats = summarize_results(rule_results, evaluator_results, n_skipped)

    from human_value._engine.llm.client import _cfg as model_defaults
    model_config = public_model_config({**model_defaults, **config.llm_options})
    model_config["retry_attempts"] = min(2, max(1, int(model_config.get("retry_attempts", 2))))
    model_config["n_runs_override"] = config.n_runs
    model_config["concurrency"] = min(20, config.concurrency or model_defaults.get("concurrency", 10))
    model_config["effective_n_runs"] = {
        ev["name"]: config.n_runs or ev.get("n_runs") or model_defaults.get("n_runs", 1)
        for ev in evaluators_all
    }
    return PipelineResult(
        rule_results=rule_results,
        llm_results=llm_results,
        llm_stats=llm_stats,
        n_skipped=n_skipped,
        report=report,
        report_rules_only=report_rules_only,
        stats=stats,
        rules=rules or [],
        evaluator_results=evaluator_results,
        model_config=model_config,
    )


def summarize_results(rule_results: list, evaluator_results: list, n_skipped: int = 0) -> dict:
    """Shared aggregation for completed evaluations and technical-error retries."""
    if rule_results:
        total  = len(rule_results)
        passed = sum(1 for r in rule_results if r["passed"])
        scores = [r["score"] for r in rule_results]
        avg    = round(sum(scores) / len(scores), 1) if scores else 0.0
    elif evaluator_results:
        # 按 trace_id 聚合：所有 evaluator 都通过才算通过，score 取均值
        # score 优先取顶层字段，无则从 rule_check.score 降级（LLM dry-run 结果无顶层 score）
        # score / passed 优先取顶层字段，无则从 rule_check 降级（hybrid dry-run 场景）
        def _ev_score_passed(r: dict):
            """返回 (score, passed) 或 None（完全无法评分时）。"""
            if r.get("execution_status") in {"technical_error", "partial_error", "missing_evidence", "dry_run"}:
                return None
            if r.get("outcome") == 0:
                return None
            s = r.get("score")
            if s is not None:
                return s, r["passed"]
            checks = [r.get("rule_check"), r.get("llm_check")]
            scored = [c for c in checks if isinstance(c, dict) and isinstance(c.get("score"), (int, float))]
            if scored:
                # llm-only binary evaluators store the score in llm_check.
                # For hybrid rows the top-level score above remains authoritative.
                return sum(c["score"] for c in scored) / len(scored), r["passed"]
            return None

        _by_trace: dict = defaultdict(list)
        for r in evaluator_results:
            _by_trace[r.get("full_trace_id") or r["trace_id"]].append(r)
        total  = len(_by_trace)
        _trace_scores = []
        _passed_count = 0
        for rs in _by_trace.values():
            _scored = [_ev_score_passed(r) for r in rs]
            _with_score = [sp for sp in _scored if sp is not None]
            if _with_score:
                _trace_pass = all(p for _, p in _with_score)
                if _trace_pass:
                    _passed_count += 1
                _trace_scores.append(sum(s for s, _ in _with_score) / len(_with_score))
        passed = _passed_count
        avg = round(sum(_trace_scores) / len(_trace_scores), 1) if _trace_scores else 0.0
    else:
        total, passed, avg = 0, 0, 0.0

    stats = {
        "total":     total,
        "passed":    passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "avg_score": avg,
        "n_skipped": n_skipped,
    }

    return stats
