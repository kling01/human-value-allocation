"""
主入口 —— 纯 IO，不含任何规则逻辑。

用法：
  python evaluate.py --data /path/to/traces.json
  python evaluate.py --data /path/to/traces.json --dates 2026-04-20,2026-04-22
  python evaluate.py --data /path/to/traces.json --exclude-contaminated
  python evaluate.py --data /path/to/traces.json --out /path/to/output/

输入：符合 TRACE_CONTRACT.md 格式的 normalized trace JSON 数组。
（由各业务适配器负责生成该文件，本脚本不关心原始格式。）
"""

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from human_value._engine.core.features import extract, get_langfuse_url
from human_value._engine.core.meta import write_meta
from human_value._engine.core.rules import RULES, PASS_THRESHOLD, apply_rules


def load_traces(path: Path, dates: list) -> list:
    with open(path, encoding="utf-8") as f:
        traces = json.load(f)
    if dates:
        traces = [t for t in traces if str(t.get("timestamp", ""))[:10] in dates]
    return traces


def run(traces: list, exclude_contaminated: bool = False, rules=None, threshold=None) -> tuple[list, int]:
    url_lookup = {str(t.get("id", ""))[:12]: get_langfuse_url(t) for t in traces}

    results = []
    n_skipped = 0
    for t in traces:
        f = extract(t)
        if f.skipped:
            n_skipped += 1
            continue
        r = apply_rules(f, trace=t, rules=rules, threshold=threshold)
        r["full_trace_id"] = str(t.get("id", ""))
        r["langfuse_url"] = url_lookup.get(f.trace_id, "")
        results.append(r)
    if exclude_contaminated:
        results = [r for r in results if not r["is_contaminated"]]
    return results, n_skipped


def make_report(results: list, meta: dict, n_skipped: int = 0, rules=None) -> str:
    active_rules = rules if rules is not None else RULES
    total = len(results)
    if total == 0:
        return "无可用数据"

    threshold = results[0].get("pass_threshold", PASS_THRESHOLD)

    contaminated = [r for r in results if r["is_contaminated"]]
    clean        = [r for r in results if not r["is_contaminated"]]
    passed       = [r for r in results if r["passed"]]
    scores       = [r["score"] for r in results]

    issue_counter = Counter(issue for r in results for issue in r["issues"])
    agent_counter = Counter(n for r in results for n in r["agent_names"])

    def pct(n, d): return f"{n}/{d} ({100*n//d if d else 0}%)"
    def avg(lst):  return f"{sum(lst)/len(lst):.1f}" if lst else "N/A"

    lines = [
        f"# Agent 架构评测报告",
        f"",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M}  ",
        f"数据集：`{meta.get('path', '')}`  ",
        f"日期范围：{meta.get('dates', '全量')}",
        f"",
        f"## 总览",
        f"",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 总 trace（原始） | {total + n_skipped} |",
        f"| 跳过（无消息） | {n_skipped} |",
        f"| 参与评测 | {total} |",
        f"| 环境污染 | {len(contaminated)} |",
        f"| 洁净 | {len(clean)} |",
        f"| 通过率 (≥{threshold}分) | {pct(len(passed), total)} |",
        f"| 平均分 | {avg(scores)} |",
        f"",
        f"> **跳过（无消息）**：trace 文件存在但 messages 为空，Agent 未正常启动，无法评测。  ",
        f"> **环境污染**：subagent 调用失败原因是插件文件缺失（Plugin file not found），属于运行环境问题而非 Agent 逻辑问题，单独标记。  ",
        f"> **洁净**：排除环境污染后可信的 trace 数量。",
        f"",
        f"## 规则触发统计",
        f"",
        f"| 规则 | 严重级别 | 扣分 | 触发 |",
        f"|------|---------|------|------|",
    ]

    for rule in active_rules:
        if not rule.enabled:
            continue
        n = issue_counter[rule.name]
        lines.append(f"| `{rule.name}` | {rule.severity} | {rule.weight:.0f} | {pct(n, total)} |")

    lines += [
        f"",
        f"## 洁净数据 (n={len(clean)})",
        f"",
    ]
    if clean:
        clean_passed = [r for r in clean if r["passed"]]
        clean_scores = [r["score"] for r in clean]
        clean_issues = Counter(i for r in clean for i in r["issues"])
        lines.append(f"通过率：{pct(len(clean_passed), len(clean))}　平均分：{avg(clean_scores)}")
        lines.append(f"")
        for rule in active_rules:
            if not rule.enabled:
                continue
            n = clean_issues[rule.name]
            if n > 0:
                lines.append(f"- `{rule.name}`：{pct(n, len(clean))}")

    lines += [
        f"",
        f"## Dispatch Agent 分布",
        f"",
        f"| Agent | 调用次数 |",
        f"|-------|---------|",
    ]
    for name, cnt in agent_counter.most_common():
        lines.append(f"| {name} | {cnt} |")

    lines += [
        f"",
        f"## 问题 Trace 明细",
        f"",
        f"| 时间 | Trace ID | 分数 | 问题 | Langfuse |",
        f"|------|---------|------|------|---------|",
    ]
    for r in sorted(results, key=lambda x: x["timestamp"]):
        if not r["issues"]:
            continue
        flag = " 🔴污染" if r["is_contaminated"] else ""
        issues_str = ", ".join(f"`{i}`" for i in r["issues"])
        url = r.get("langfuse_url", "")
        link = f"[查看]({url})" if url else "—"
        lines.append(
            f"| {r['timestamp'][:16]} | `{r['trace_id']}`{flag} "
            f"| {r['score']:.0f} | {issues_str} | {link} |"
        )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Agent 架构通用评测框架")
    parser.add_argument("--data",  required=True, help="normalized trace JSON 路径（数组格式）")
    parser.add_argument("--dates", default=None,  help="逗号分隔日期，如 2026-04-20,2026-04-21")
    parser.add_argument("--out",   default="./output", help="输出根目录，实际写入 <out>/<YYYYMMDD_HHMM>/（默认 ./output）")
    parser.add_argument("--exclude-contaminated", action="store_true", help="排除环境污染数据不参与统计")
    args = parser.parse_args()

    dates  = [d.strip() for d in args.dates.split(",")] if args.dates else None
    traces = load_traces(Path(args.data), dates)
    results, n_skipped = run(traces, exclude_contaminated=args.exclude_contaminated)

    meta   = {"path": args.data, "dates": args.dates or "全量"}
    report = make_report(results, meta, n_skipped=n_skipped)
    print(report)

    ts      = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = Path(args.out) / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report_rules_only.md").write_text(report, encoding="utf-8")
    with open(out_dir / "detail.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    meta = write_meta(out_dir, Path(args.data), stage="rules", n_traces=len(traces))

    print(f"\n报告 → {out_dir / 'report_rules_only.md'}")
    print(f"明细 → {out_dir / 'detail.json'}")
    print(f"数据源 hash → {meta['source_hash']}  ({meta['source_file']})")


if __name__ == "__main__":
    main()
