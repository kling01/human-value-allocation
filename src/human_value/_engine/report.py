"""
统一报告生成器 —— 合并规则评测结果与 LLM 评测结果。

用法：
  # 只看规则
  python report.py --rule ./output/travel/detail.json --out ./output/travel/report.md

  # 只看 LLM
  python report.py --llm ./output/travel/llm_results.json --out ./output/travel/report.md

  # 合并两者
  python report.py --rule ./output/travel/detail.json --llm ./output/travel/llm_results.json --out ./output/travel/report.md

输入：
  detail.json      —— evaluate.py 输出的规则评测结果（每条 trace 一个 dict）
  llm_results.json —— llm_eval.py 输出的 LLM 评测结果（每条 trace 一个 dict）

合并键：trace_id（取前 12 位）
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from human_value._engine.core.meta import read_meta


# ── 数据源一致性校验 ───────────────────────────────────────────────────────────

def check_source_consistency(rule_path: Optional[str], llm_path: Optional[str]) -> None:
    """
    比对 detail.json 和 llm_results.json 的数据源 hash。
    - 两者 hash 一致：打印确认信息
    - 不一致：打印警告（不阻断，继续生成报告）
    - 任一缺少 _meta.json（旧版输出）：提示但不报错
    """
    if not rule_path or not llm_path:
        return

    rule_meta = read_meta(Path(rule_path))
    llm_meta  = read_meta(Path(llm_path))

    if rule_meta is None and llm_meta is None:
        print("ℹ️  两个输入目录均无 _meta.json，跳过数据源校验（旧版输出）")
        return

    if rule_meta is None:
        print("⚠️  规则结果目录无 _meta.json，无法校验数据源一致性")
        return

    if llm_meta is None:
        print("⚠️  LLM 结果目录无 _meta.json，无法校验数据源一致性")
        return

    if rule_meta["source_hash"] == llm_meta["source_hash"]:
        print(
            f"✅ 数据源一致  hash={rule_meta['source_hash']}"
            f"  ({rule_meta['source_file']})"
        )
    else:
        print("=" * 60)
        print("⚠️  警告：规则结果与 LLM 结果来自不同数据源！报告可能混淆数据！")
        print(f"   规则: {rule_meta['source_file']}")
        print(f"         hash={rule_meta['source_hash']}  生成于 {rule_meta['generated_at']}")
        print(f"   LLM:  {llm_meta['source_file']}")
        print(f"         hash={llm_meta['source_hash']}  生成于 {llm_meta['generated_at']}")
        print("=" * 60)


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_rule_results(path: str) -> dict:
    """加载规则评测结果，返回 {trace_id: record} 字典。"""
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    return {r.get("full_trace_id", r["trace_id"]): r for r in records}


def load_llm_results(path: str) -> dict:
    """加载 LLM 评测结果，返回 {trace_id: {evaluator: record}} 字典。"""
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    result = defaultdict(dict)
    for r in records:
        tid = r.get("full_trace_id", r["trace_id"])
        ev  = r.get("evaluator", "LLM")
        result[tid][ev] = r
    return dict(result)


# ── 报告渲染 ──────────────────────────────────────────────────────────────────

def _fmt_bool(v: bool) -> str:
    return "✅ 通过" if v else "❌ 失败"


def _fmt_llm_passed(v: int) -> str:
    if v == 1:
        return "✅ 通过"
    if v == 0:
        return "❌ 失败"
    return "⚠️ 错误"


def _rule_score(record: dict) -> str:
    score = record.get("score", 0)
    threshold = record.get("pass_threshold", 60)
    mark = "✅" if record.get("passed") else "❌"
    return f"{mark} {score}/{threshold}"


def render_report(
    rule_data: Optional[dict],
    llm_data:  Optional[dict],
    rule_path: str = "",
    llm_path:  str = "",
) -> str:
    lines = [
        "# Agent 评测报告",
        "",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M}",
    ]
    if rule_path:
        lines.append(f"规则结果：`{rule_path}`  ")
    if llm_path:
        lines.append(f"LLM 结果：`{llm_path}`  ")
    lines.append("")

    # ── 概览 ─────────────────────────────────────────────────────────────────
    lines += ["## 概览", ""]

    if rule_data:
        total = len(rule_data)
        passed = sum(1 for r in rule_data.values() if r.get("passed"))
        lines += [
            f"**规则评测**：{total} 条 trace，通过 {passed} 条（{passed/total*100:.1f}%）",
            "",
        ]

    if llm_data:
        # 按评估器分组统计（含稳定性）
        ev_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "passed": 0, "stable": 0, "has_stability": False})
        for tid, ev_map in llm_data.items():
            for ev_name, rec in ev_map.items():
                ev_stats[ev_name]["total"] += 1
                if rec.get("passed"):
                    ev_stats[ev_name]["passed"] += 1
                stab = rec.get("stability")
                if stab is not None:
                    ev_stats[ev_name]["has_stability"] = True
                    if stab.get("is_stable"):
                        ev_stats[ev_name]["stable"] += 1

        has_any_stability = any(st["has_stability"] for st in ev_stats.values())

        lines.append("**LLM 评测**（按评估器）：")
        lines.append("")
        header = "| 评估器 | Trace 数 | 通过 | 通过率 |"
        sep    = "|--------|---------|------|--------|"
        if has_any_stability:
            header += " 稳定率 |"
            sep    += "--------|"
        lines.append(header)
        lines.append(sep)
        for ev_name, st in ev_stats.items():
            pct = st["passed"] / st["total"] * 100 if st["total"] else 0
            row = f"| {ev_name} | {st['total']} | {st['passed']} | {pct:.1f}% |"
            if has_any_stability:
                if st["has_stability"]:
                    spct = st["stable"] / st["total"] * 100 if st["total"] else 0
                    row += f" {st['stable']}/{st['total']} ({spct:.1f}%) |"
                else:
                    row += " — |"
            lines.append(row)
        lines.append("")

    # ── 合并明细表 ────────────────────────────────────────────────────────────
    lines += ["## 明细", ""]

    all_ids = sorted(set(
        list(rule_data.keys() if rule_data else []) +
        list(llm_data.keys()  if llm_data  else [])
    ))

    if not all_ids:
        lines += ["（无数据）", ""]
        return "\n".join(lines)

    # 收集所有 LLM 评估器名
    ev_names: list[str] = []
    if llm_data:
        seen: set[str] = set()
        for ev_map in llm_data.values():
            for n in ev_map:
                if n not in seen:
                    ev_names.append(n)
                    seen.add(n)

    # 表头
    header_cols = ["Trace ID"]
    if rule_data:
        header_cols.append("规则得分")
    for ev in ev_names:
        header_cols.append(f"{ev} 通过率")
        header_cols.append(f"{ev} Step Pass")
    header_cols.append("Langfuse")

    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("|" + "|".join("-" * (len(c) + 2) for c in header_cols) + "|")

    for tid in all_ids:
        row = [f"`{tid}`"]

        if rule_data:
            rec = rule_data.get(tid)
            row.append(_rule_score(rec) if rec else "—")

        langfuse_url = ""
        for ev in ev_names:
            ev_rec = (llm_data.get(tid) or {}).get(ev)
            if ev_rec:
                if not langfuse_url:
                    langfuse_url = ev_rec.get("langfuse_url", "")
                n_steps  = ev_rec.get("n_steps", 0)
                n_passed = ev_rec.get("n_passed", 0)
                pct      = f"{n_passed}/{n_steps}" if n_steps else "—"
                passed_str = _fmt_bool(ev_rec.get("passed", False))
                stab = ev_rec.get("stability")
                if stab is not None and not stab.get("is_stable"):
                    passed_str += " ⚠️"
                row.append(passed_str)
                row.append(pct)
            else:
                row += ["—", "—"]

        if not langfuse_url and rule_data:
            rec = rule_data.get(tid)
            langfuse_url = (rec or {}).get("langfuse_url", "")

        link = f"[↗]({langfuse_url})" if langfuse_url else "—"
        row.append(link)
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")

    # ── LLM 失败详情 ──────────────────────────────────────────────────────────
    if llm_data:
        lines += ["## LLM 评测失败详情", ""]
        has_failure = False
        for tid in all_ids:
            for ev in ev_names:
                ev_rec = (llm_data.get(tid) or {}).get(ev)
                if not ev_rec or ev_rec.get("passed"):
                    continue
                has_failure = True
                langfuse_url = ev_rec.get("langfuse_url", "")
                link = f" [↗]({langfuse_url})" if langfuse_url else ""
                lines += [f"### `{tid}`{link} — {ev}", ""]
                for step in ev_rec.get("steps", []):
                    passed_mark = "✅" if step.get("passed") == 1 else "❌" if step.get("passed") == 0 else "⚠️"
                    analysis = (json.dumps(step.get("analysis"), ensure_ascii=False) if isinstance(step.get("analysis"), (dict, list)) else str(step.get("analysis") or "")).strip()
                    reason   = step.get("reason", "")
                    detail = f"{analysis}  \n  {reason}" if analysis else reason
                    lines.append(
                        f"- Step {step.get('step')} `{step.get('agent_name', '?')}`："
                        f"{passed_mark} {detail}"
                    )
                lines.append("")

        if not has_failure:
            lines += ["（全部通过）", ""]

    # ── LLM 稳定性分析 ────────────────────────────────────────────────────────
    if llm_data:
        unstable_records = []
        for tid in all_ids:
            for ev in ev_names:
                ev_rec = (llm_data.get(tid) or {}).get(ev)
                if not ev_rec:
                    continue
                stab = ev_rec.get("stability")
                if stab and not stab.get("is_stable"):
                    unstable_records.append((tid, ev, ev_rec, stab))

        # 找出第一条有 stability 的记录，确认 n_runs 配置
        sample_n_runs = None
        for tid in all_ids:
            for ev in ev_names:
                ev_rec = (llm_data.get(tid) or {}).get(ev)
                if ev_rec and ev_rec.get("stability"):
                    sample_n_runs = ev_rec["stability"].get("n_runs")
                    break
            if sample_n_runs:
                break

        if sample_n_runs and sample_n_runs > 1:
            total_with_stab = sum(
                1 for tid in all_ids
                for ev in ev_names
                if (llm_data.get(tid) or {}).get(ev, {}).get("stability") is not None
            )
            lines += [
                "## LLM 稳定性分析",
                "",
                f"> 每条 trace 重复调用 {sample_n_runs} 次，以多数投票决定最终结果。",
                f"> ⚠️ 表示本次 {sample_n_runs} 次结果不完全一致，说明该评测存在不确定性。",
                "",
                f"**不稳定 trace**：{len(unstable_records)}/{total_with_stab}",
                "",
            ]
            if unstable_records:
                lines += [
                    "| Trace ID | 评估器 | 多数结果 | 一致率 | 各次结果 | Langfuse |",
                    "|---------|--------|---------|--------|---------|---------|",
                ]
                for tid, ev, ev_rec, stab in unstable_records:
                    langfuse_url = ev_rec.get("langfuse_url", "")
                    link = f"[↗]({langfuse_url})" if langfuse_url else "—"
                    majority_str = "✅ 通过" if ev_rec.get("passed") else "❌ 失败"
                    agree_rate = f"{stab.get('n_agree')}/{stab.get('n_runs')} ({stab.get('agreement_rate', 0)*100:.0f}%)"
                    runs_str = " / ".join(
                        ("✅" if r["passed"] == 1 else "❌" if r["passed"] == 0 else "⚠️")
                        for r in stab.get("run_results", [])
                    )
                    lines.append(f"| `{tid}` | {ev} | {majority_str} | {agree_rate} | {runs_str} | {link} |")
                lines.append("")

                lines.append("### 不稳定 Trace 各次理由")
                lines.append("")
                for tid, ev, ev_rec, stab in unstable_records:
                    langfuse_url = ev_rec.get("langfuse_url", "")
                    link = f" [↗]({langfuse_url})" if langfuse_url else ""
                    lines += [f"#### `{tid}`{link} — {ev}", ""]
                    for r in stab.get("run_results", []):
                        mark = "✅" if r["passed"] == 1 else "❌" if r["passed"] == 0 else "⚠️"
                        analysis = (json.dumps(r.get("analysis"), ensure_ascii=False) if isinstance(r.get("analysis"), (dict, list)) else str(r.get("analysis") or "")).strip()
                        reason   = r.get("reason", "")
                        detail   = f"{analysis}  {reason}" if analysis else reason
                        lines.append(f"- 第 {r['run']} 次 {mark}：{detail}")
                    lines.append("")
            else:
                lines += ["（全部稳定）", ""]

    # ── 规则失败详情 ──────────────────────────────────────────────────────────
    if rule_data:
        failed = {tid: r for tid, r in rule_data.items() if not r.get("passed")}
        if failed:
            lines += ["## 规则评测失败详情", ""]
            for tid, rec in failed.items():
                langfuse_url = rec.get("langfuse_url", "")
                link = f" [↗]({langfuse_url})" if langfuse_url else ""
                lines += [f"### `{tid}`{link}", ""]
                issue_detail = rec.get("issue_detail", {})
                for issue_name in rec.get("issues", []):
                    desc = issue_detail.get(issue_name, "")
                    lines.append(f"- **{issue_name}**：{desc}")
                lines.append("")

    return "\n".join(lines)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="合并规则 + LLM 评测，输出统一报告")
    parser.add_argument("--rule", default=None, help="detail.json 路径")
    parser.add_argument("--llm",  default=None, help="llm_results.json 路径")
    parser.add_argument("--out",  default=None, help="输出 Markdown 路径（默认打印）")
    parser.add_argument("--html", default=None, help="同时输出 HTML 报告路径（可选）")
    args = parser.parse_args()

    if not args.rule and not args.llm:
        parser.error("至少提供 --rule 或 --llm 之一")

    check_source_consistency(args.rule, args.llm)

    rule_data = load_rule_results(args.rule) if args.rule else None
    llm_data  = load_llm_results(args.llm)  if args.llm  else None

    report = render_report(rule_data, llm_data, args.rule or "", args.llm or "")
    print(report)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"\n报告 → {args.out}")

    if args.html:
        from human_value._engine.report_html import render_html_report
        html_report = render_html_report(rule_data, llm_data, args.rule or "", args.llm or "")
        Path(args.html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.html).write_text(html_report, encoding="utf-8")
        print(f"HTML  → {args.html}")


if __name__ == "__main__":
    main()
