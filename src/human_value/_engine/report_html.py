"""
HTML 报告生成器 —— 输出自包含 HTML 文件（无外部依赖，可直接打开）。

用法（模块）：
  from human_value._engine.report_html import render_html_report

用法（命令行）：
  python report_html.py --rule ./output/detail.json --out ./output/report.html
  python report_html.py --llm  ./output/llm_results.json --out ./output/report.html
  python report_html.py --rule ./output/detail.json --llm ./output/llm_results.json --out ./output/report.html
"""

from __future__ import annotations

import argparse
import html as _html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 严重级别 ──────────────────────────────────────────────────────────────────

_SEV_ORDER = {"critical": 0, "warning": 1, "info": 2, "unknown": 3}

_SEV_STYLE = {
    # (text, bg, border, dot, label)
    "critical": ("#991b1b", "#fff1f2", "#fecdd3", "#ef4444", "严重"),
    "warning":  ("#92400e", "#fffbeb", "#fde68a", "#f59e0b", "警告"),
    "info":     ("#1e40af", "#eff6ff", "#bfdbfe", "#3b82f6", "提示"),
    "unknown":  ("#374151", "#f9fafb", "#e5e7eb", "#9ca3af", "未知"),
}


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return _html.escape(str(s) if s is not None else "")


def _load_all_rules() -> list:
    try:
        import yaml
        p = Path(__file__).parent / "config" / "common" / "rules.yaml"
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        return [
            {
                "id":          r.get("id", r["name"]),
                "name":        r["name"],
                "description": r.get("description", ""),
                "severity":    r.get("severity", "unknown"),
                "enabled":     r.get("enabled", True),
                "weight":      r.get("weight", 0),
            }
            for r in cfg.get("rules", [])
        ]
    except Exception:
        return []


def _sev_badge(severity: str) -> str:
    s = _SEV_STYLE.get(severity, _SEV_STYLE["unknown"])
    return (
        f'<span class="badge" style="color:{s[0]};background:{s[1]};border-color:{s[2]}">'
        f'<span style="width:6px;height:6px;border-radius:50%;background:{s[3]};display:inline-block;margin-right:4px;vertical-align:middle"></span>'
        f'{s[4]}</span>'
    )


def _langfuse_btn(url: str) -> str:
    if not url:
        return '<span class="null-dash">—</span>'
    return (
        f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer" class="lf-btn">'
        f'Langfuse ↗</a>'
    )


def _trace_btn(tid: str) -> str:
    """指向自有 trace 可视化页。落盘时 __RUN_ID__→真实 run_id、__TRACE_BASE__→公网前缀
    （storage._save 用环境变量 PUBLIC_TRACE_BASE 替换；未设则为空=相对路径，仅本机可点）。
    设为服务器前端地址（如 http://localhost:8000）后，报告分享出去别人也能点开轨迹。"""
    if not tid:
        return '<span class="null-dash">—</span>'
    return (
        f'<a href="__TRACE_BASE__/trace/__RUN_ID__/{_e(tid)}" target="_blank" rel="noopener noreferrer" class="lf-btn">'
        f'查看轨迹 ↗</a>'
    )


def _pct_color(pct: float) -> str:
    if pct >= 100:
        return "#16a34a"
    if pct >= 80:
        return "#ca8a04"
    if pct >= 60:
        return "#ea580c"
    return "#dc2626"


def _rate_bar(passed: int, total: int, width: int = 120) -> str:
    if total == 0:
        return "—"
    pct   = passed / total * 100
    color = _pct_color(pct)
    fill  = max(2, int(width * pct / 100))
    badge = ' <span class="all-pass-tag">全部通过</span>' if pct == 100 else ""
    return (
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<div style="width:{width}px;height:6px;background:#f1f5f9;border-radius:3px;overflow:hidden;flex-shrink:0">'
        f'<div style="width:{fill}px;height:6px;background:{color};border-radius:3px"></div>'
        f'</div>'
        f'<span style="font-size:12px;color:{color};font-weight:600;white-space:nowrap">'
        f'{passed}/{total} ({pct:.1f}%)</span>{badge}'
        f'</div>'
    )


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """
*, *::before, *::after { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, sans-serif;
  font-size: 13px;
  line-height: 1.6;
  background: #f1f5f9;
  color: #0f172a;
  margin: 0;
}

/* ── header ── */
.top-bar {
  background: #0f172a;
  color: #f8fafc;
  padding: 0 28px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 200;
  box-shadow: 0 1px 0 rgba(255,255,255,.06), 0 2px 12px rgba(0,0,0,.3);
}
.top-bar-title {
  font-size: 15px;
  font-weight: 700;
  letter-spacing: -.3px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.top-bar-title::before {
  content: '';
  display: inline-block;
  width: 8px; height: 8px;
  background: #22d3ee;
  border-radius: 50%;
}
.top-bar-meta {
  font-size: 11px;
  color: #64748b;
  display: flex;
  gap: 16px;
}
.top-bar-meta span { display: flex; align-items: center; gap: 4px; }

/* ── layout ── */
.page { max-width: 1160px; margin: 0 auto; padding: 24px 20px 80px; }

/* ── summary cards ── */
.cards-row {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.card {
  background: white;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 16px 18px;
  box-shadow: 0 1px 4px rgba(0,0,0,.04);
  position: relative;
  overflow: hidden;
}
.card::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  border-radius: 12px 12px 0 0;
}
.card.green::after  { background: #22c55e; }
.card.amber::after  { background: #f59e0b; }
.card.orange::after { background: #ea580c; }
.card.red::after    { background: #ef4444; }
.card-label  { font-size: 11px; color: #64748b; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 6px; }
.card-value  { font-size: 28px; font-weight: 800; line-height: 1; margin-bottom: 4px; }
.card-sub    { font-size: 11px; color: #94a3b8; }

/* ── panel ── */
.panel {
  background: white;
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  margin-bottom: 16px;
  box-shadow: 0 1px 4px rgba(0,0,0,.04);
  overflow: hidden;
}
.panel-header {
  padding: 14px 20px;
  display: flex;
  align-items: center;
  gap: 10px;
  border-bottom: 1px solid #f1f5f9;
  background: #fafafa;
}
.panel-title { font-size: 14px; font-weight: 700; color: #0f172a; }
.panel-count { font-size: 12px; color: #64748b; background: #f1f5f9; padding: 2px 8px; border-radius: 20px; }

/* ── subsection ── */
.subsection { border-top: 1px solid #f1f5f9; }
.subsection:first-child { border-top: none; }
.sub-header {
  padding: 11px 20px;
  background: #fafafa;
  border-bottom: 1px solid #f1f5f9;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.sub-title {
  font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace;
  font-size: 12px;
  font-weight: 700;
  color: #1e293b;
}
.sub-desc { font-size: 12px; color: #64748b; flex: 1; min-width: 0; }

/* ── tables ── */
table { width: 100%; border-collapse: collapse; }
th {
  padding: 8px 14px;
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: .06em;
  text-align: left;
  background: #f8fafc;
  border-bottom: 1px solid #e2e8f0;
  white-space: nowrap;
}
th:first-child { padding-left: 20px; }
th:last-child  { padding-right: 20px; }
td {
  padding: 9px 14px;
  border-bottom: 1px solid #f8fafc;
  vertical-align: middle;
}
td:first-child { padding-left: 20px; }
td:last-child  { padding-right: 20px; }
tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: #f8fafc; }

/* severity row accent */
.row-critical td:first-child { border-left: 3px solid #ef4444; padding-left: 17px; }
.row-warning  td:first-child { border-left: 3px solid #f59e0b; padding-left: 17px; }
.row-info     td:first-child { border-left: 3px solid #3b82f6; padding-left: 17px; }
.row-unknown  td:first-child { border-left: 3px solid #d1d5db; padding-left: 17px; }

/* ── misc components ── */
.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 600;
  border: 1px solid;
  white-space: nowrap;
}
.count-chip {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 20px;
  font-size: 11px;
  font-weight: 700;
  background: #fff1f2;
  color: #991b1b;
  border: 1px solid #fecdd3;
}
.all-pass-row td { background: #f0fdf4; }
.all-pass-tag {
  display: inline-block;
  padding: 1px 7px;
  border-radius: 20px;
  font-size: 10px;
  font-weight: 700;
  background: #dcfce7;
  color: #166534;
  border: 1px solid #bbf7d0;
  white-space: nowrap;
}
.tid {
  font-family: "SF Mono", "Fira Code", monospace;
  font-size: 11px;
  color: #334155;
  background: #f1f5f9;
  padding: 2px 7px;
  border-radius: 4px;
  white-space: nowrap;
  letter-spacing: .03em;
}
.reason-cell { color: #334155; max-width: 620px; }
.vio { display:flex; gap:8px; align-items:flex-start; padding:7px 0; border-top:1px dashed #e6e9f0; }
.vio:first-child { border-top:none; padding-top:2px; }
.v-lv { flex:none; font-size:10px; font-weight:800; border-radius:4px; padding:1px 7px; margin-top:2px;
        font-family:'SF Mono',ui-monospace,Consolas,monospace; }
.vio.p0 .v-lv { color:#b91c1c; background:#fee2e2; }
.vio.p1 .v-lv { color:#b45309; background:#fef3c7; }
.vio.p2 .v-lv { color:#475569; background:#e2e8f0; }
.v-body { min-width:0; line-height:1.6; }
.v-doc { color:#1d4ed8; font-size:12.5px; }
.v-act { color:#0f172a; font-size:12.5px; margin-top:1px; }
.v-why { color:#64748b; font-size:12px; margin-top:1px; }
.lf-btn {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  background: #f0f9ff;
  border: 1px solid #7dd3fc;
  color: #0284c7;
  text-decoration: none;
  white-space: nowrap;
  transition: background .15s;
}
.lf-btn:hover { background: #e0f2fe; }
.null-dash { color: #cbd5e1; }
.all-pass-msg {
  padding: 16px 20px;
  color: #16a34a;
  font-size: 13px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
}
.all-pass-msg::before { content: '✓'; font-weight: 800; font-size: 15px; }
.monospace { font-family: "SF Mono", "Fira Code", monospace; font-size: 12px; }
.section-gap { margin-top: 8px; }
/* 每评估器下按三态分的可折叠组 */
.tri-split { font-size: 12px; color: #64748b; margin-left: 10px; }
.tri-split b { font-weight: 700; }
.tri-split .t-pass { color: #16a34a; } .tri-split .t-na { color: #94a3b8; }
.tri-split .t-fail { color: #ef4444; } .tri-split .t-err { color: #ea580c; }
.oc-group { margin: 6px 20px; border: 1px solid #eef2f7; border-radius: 8px; overflow: hidden; }
.oc-group > summary {
  cursor: pointer; padding: 8px 14px; font-size: 13px; font-weight: 600;
  list-style: none; user-select: none; display: flex; align-items: center; gap: 8px;
}
.oc-group > summary::-webkit-details-marker { display: none; }
.oc-group > summary::before { content: '▸'; font-size: 11px; transition: transform .15s; color: #94a3b8; }
.oc-group[open] > summary::before { transform: rotate(90deg); }
.oc-fail > summary { background: #fef2f2; color: #b91c1c; }
.oc-err  > summary { background: #fff7ed; color: #c2410c; }
.oc-na   > summary { background: #f8fafc; color: #64748b; }
.oc-pass > summary { background: #f0fdf4; color: #15803d; }
.oc-group table { margin: 0; }
"""


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_rule_results(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    return {r.get("full_trace_id", r["trace_id"]): r for r in records}


def load_llm_results(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    result = defaultdict(dict)
    for r in records:
        tid = r.get("full_trace_id", r["trace_id"])
        ev  = r.get("evaluator", "LLM")
        result[tid][ev] = r
    return dict(result)


# ── 渲染：概览卡片 ────────────────────────────────────────────────────────────

def _card_color_cls(pct: float) -> str:
    if pct >= 100: return "green"
    if pct >= 80:  return "amber"
    if pct >= 60:  return "orange"
    return "red"


def _render_cards(rule_data: Optional[dict], llm_data: Optional[dict]) -> str:
    parts = []

    if rule_data:
        total  = len(rule_data)
        passed = sum(1 for r in rule_data.values() if r.get("passed"))
        pct    = passed / total * 100 if total else 0
        color  = _pct_color(pct)
        cls    = _card_color_cls(pct)
        parts.append(
            f'<div class="card {cls}">'
            f'<div class="card-label">规则评测</div>'
            f'<div class="card-value" style="color:{color}">{pct:.1f}%</div>'
            f'<div class="card-sub">{passed} / {total} 通过</div>'
            f'</div>'
        )

    if llm_data:
        ev_stats: dict = defaultdict(lambda: {"total": 0, "passed": 0})
        for ev_map in llm_data.values():
            for ev_name, rec in ev_map.items():
                ev_stats[ev_name]["total"] += 1
                if rec.get("passed"):
                    ev_stats[ev_name]["passed"] += 1
        for ev_name, st in ev_stats.items():
            t   = st["total"]
            p   = st["passed"]
            pct = p / t * 100 if t else 0
            clr = _pct_color(pct)
            cls = _card_color_cls(pct)
            parts.append(
                f'<div class="card {cls}">'
                f'<div class="card-label">{_e(ev_name)}</div>'
                f'<div class="card-value" style="color:{clr}">{pct:.1f}%</div>'
                f'<div class="card-sub">{p} / {t} 通过</div>'
                f'</div>'
            )

    return f'<div class="cards-row">{"".join(parts)}</div>'


# ── 渲染：规则统计表 ──────────────────────────────────────────────────────────

def _render_rule_stats_table(rule_data: dict, all_rules: list) -> str:
    total = len(rule_data)

    trigger_count: dict = defaultdict(int)
    rule_desc: dict = {}
    for rec in rule_data.values():
        for name in rec.get("issues", []):
            trigger_count[name] += 1
        for name, desc in (rec.get("issue_detail") or {}).items():
            rule_desc[name] = desc

    # 合并 yaml 规则 + 数据里出现的额外规则
    known = {r.get("id", r["name"]) for r in all_rules}
    rows  = [r for r in all_rules if r.get("enabled", True)]
    for rule_id in trigger_count:
        if rule_id not in known:
            rows.append({"id": rule_id, "name": rule_id, "description": rule_desc.get(rule_id, ""), "severity": "unknown", "enabled": True, "weight": 0})

    rows.sort(key=lambda r: (_SEV_ORDER.get(r["severity"], 3), -trigger_count.get(r.get("id", r["name"]), 0)))

    parts = [
        '<table><thead><tr>',
        '<th>级别</th><th>规则名</th><th>说明</th>',
        '<th style="text-align:right">触发</th>',
        '<th>通过率</th>',
        '</tr></thead><tbody>',
    ]
    for r in rows:
        rule_id = r.get("id", r["name"])
        name = r["name"]
        sev  = r.get("severity", "unknown")
        desc = r.get("description") or rule_desc.get(rule_id, "")
        cnt  = trigger_count.get(rule_id, 0)
        passed = total - cnt
        row_cls = f"row-{sev}"
        all_ok  = cnt == 0

        parts.append(
            f'<tr class="{row_cls}{" all-pass-row" if all_ok else ""}">'
            f'<td>{_sev_badge(sev)}</td>'
            f'<td><span class="monospace" style="font-weight:700">{_e(name)}</span></td>'
            f'<td style="color:#475569;font-size:12px">{_e(desc)}</td>'
            f'<td style="text-align:right">'
            + (f'<span class="count-chip">{cnt}</span>' if cnt > 0 else '<span style="color:#94a3b8">0</span>') +
            f'</td>'
            f'<td>{_rate_bar(passed, total, 100)}</td>'
            f'</tr>'
        )
    parts.append('</tbody></table>')
    return "\n".join(parts)


# ── 渲染：规则错误明细 ────────────────────────────────────────────────────────

def _render_rule_failures(rule_data: dict, all_rules: list) -> str:
    rule_to_traces: dict = defaultdict(list)
    rule_desc: dict = {}
    for tid, rec in rule_data.items():
        url = rec.get("langfuse_url", "")
        for name in rec.get("issues", []):
            desc = (rec.get("issue_detail") or {}).get(name, "")
            rule_to_traces[name].append({"tid": tid, "desc": desc, "url": url})
        for name, desc in (rec.get("issue_detail") or {}).items():
            rule_desc[name] = desc

    if not rule_to_traces:
        return '<div class="all-pass-msg">全部通过，无触发规则</div>'

    sev_map = {r.get("id", r["name"]): r.get("severity", "unknown") for r in all_rules}
    sorted_rules = sorted(
        rule_to_traces.keys(),
        key=lambda n: (_SEV_ORDER.get(sev_map.get(n, "unknown"), 3), -len(rule_to_traces[n]))
    )

    parts = []
    for name in sorted_rules:
        items = rule_to_traces[name]
        sev   = sev_map.get(name, "unknown")
        desc  = rule_desc.get(name, "")

        parts.append(
            f'<div class="subsection">'
            f'<div class="sub-header">'
            f'<span class="sub-title">{_e(name)}</span>'
            f'{_sev_badge(sev)}'
            f'<span class="count-chip">{len(items)} 条</span>'
            f'<span class="sub-desc">{_e(desc)}</span>'
            f'</div>'
            f'<table><thead><tr><th>Trace ID</th><th>触发描述</th><th>追踪</th></tr></thead><tbody>'
        )
        for item in items:
            link = _langfuse_btn(item["url"]) if item.get("url") else _trace_btn(item["tid"])
            parts.append(
                f'<tr>'
                f'<td><span class="tid">{_e(item["tid"])}</span></td>'
                f'<td class="reason-cell">{_e(item["desc"])}</td>'
                f'<td>{link}</td>'
                f'</tr>'
            )
        parts.append('</tbody></table></div>')

    return "\n".join(parts)


# ── 渲染：LLM 评估器 ──────────────────────────────────────────────────────────

def _render_llm_evaluators(llm_data: dict) -> str:
    ev_names: list = []
    seen: set = set()
    for ev_map in llm_data.values():
        for n in ev_map:
            if n not in seen:
                ev_names.append(n)
                seen.add(n)

    if not ev_names:
        return '<p style="padding:16px 20px;color:#94a3b8">（无 LLM 评测数据）</p>'

    # 每评估器下按三态分组：-1 错误(默认展开) / None 未跑通(默认展开) / 0 无关(折叠) / 1 正确(折叠)
    #   (outcome, 标签, css类, 默认展开)
    OUTCOME_GROUPS = [
        (-1,   "错误 FAIL",     "oc-fail", True),
        (None, "未跑通(接口报错)", "oc-err",  True),
        (0,    "无关 N/A",      "oc-na",   False),
        (1,    "正确 PASS",     "oc-pass", False),
    ]

    def _fmt_reason(s: str) -> str:
        """[P0]/[P1]/[P2] 分级格式 → 按条分行渲染；非该格式原样返回(已转义)。"""
        import re as _re
        if not s or not _re.search(r'\[P[0-2]\]', s):
            return _e(s)
        items = [x.strip(' ；;') for x in _re.split(r'(?=\[P[0-2]\])', s) if x.strip(' ；;')]
        out = []
        for it in items:
            m = _re.match(r'\[P([0-2])\]\s*(.*)$', it, _re.S)
            if not m:
                out.append(f'<div class="vio">{_e(it)}</div>')
                continue
            lv, body = m.group(1), m.group(2).strip()
            segs = [x.strip() for x in body.split('｜') if x.strip()]
            if len(segs) >= 3:
                inner = (f'<div class="v-doc">{_e(segs[0])}</div>'
                         f'<div class="v-act">{_e(segs[1])}</div>'
                         f'<div class="v-why">{_e("｜".join(segs[2:]))}</div>')
            else:
                inner = f'<div class="v-act">{_e(body)}</div>'
            out.append(f'<div class="vio p{lv}"><span class="v-lv">P{lv}</span><div class="v-body">{inner}</div></div>')
        return "".join(out)

    def _reason_of(rec: dict) -> str:
        for step in rec.get("steps", []):
            return step.get("reason") or step.get("analysis") or ""
        return ""

    def _group_key(rec: dict):
        """归入三态桶。outcome 缺失(二值评估器)时看 steps[0].passed：
        -1=接口报错(未跑通) / 1=通过归 PASS / 0=不通过归 FAIL。"""
        oc = rec.get("outcome")
        if oc in (-1, 0, 1):
            return oc
        raw = (rec.get("steps") or [{}])[0].get("passed")
        if raw == -1:
            return None                     # 错误哨兵 → 未跑通
        if raw == 1 or rec.get("passed") is True:
            return 1                        # 二值通过
        return -1                           # 二值不通过

    parts = []
    for ev_name in ev_names:
        buckets: dict = {-1: [], None: [], 0: [], 1: []}
        for tid, ev_map in llm_data.items():
            rec = ev_map.get(ev_name)
            if not rec:
                continue
            buckets[_group_key(rec)].append({"tid": tid, "reason": _reason_of(rec), "url": rec.get("langfuse_url", "")})

        n_pass, n_na, n_fail, n_err = len(buckets[1]), len(buckets[0]), len(buckets[-1]), len(buckets[None])
        total = n_pass + n_na + n_fail + n_err
        tri = (f'<span class="tri-split">'
               f'<b class="t-pass">✓正确 {n_pass}</b> · '
               f'<b class="t-na">无关 {n_na}</b> · '
               f'<b class="t-fail">✗错误 {n_fail}</b>'
               + (f' · <b class="t-err">未跑通 {n_err}</b>' if n_err else '')
               + '</span>')
        parts.append(
            f'<div class="subsection">'
            f'<div class="sub-header">'
            f'<span class="sub-title">{_e(ev_name)}</span>'
            f'<span style="font-size:13px;display:flex;align-items:center">{_rate_bar(n_pass, total, 100)}{tri}</span>'
            f'</div>'
        )
        for oc_key, label, css, default_open in OUTCOME_GROUPS:
            rows = buckets[oc_key]
            if not rows:
                continue
            parts.append(f'<details class="oc-group {css}"{" open" if default_open else ""}>')
            parts.append(f'<summary>{label} · {len(rows)} 条</summary>')
            parts.append('<table><thead><tr><th>Trace ID</th><th>结论</th><th>追踪</th></tr></thead><tbody>')
            for r in rows:
                parts.append(
                    f'<tr>'
                    f'<td><span class="tid">{_e(r["tid"])}</span></td>'
                    f'<td class="reason-cell">{_fmt_reason(r["reason"])}</td>'
                    f'<td>{_trace_btn(r["tid"])}</td>'
                    f'</tr>'
                )
            parts.append('</tbody></table></details>')
        parts.append('</div>')

    return "\n".join(parts)


# ── 主渲染函数 ────────────────────────────────────────────────────────────────

def render_html_report(
    rule_data: Optional[dict],
    llm_data:  Optional[dict],
    rules=None,
    rule_path: str = "",
    llm_path:  str = "",
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    all_rules = (
        [
            {
                "name":        r.name,
                "description": r.description,
                "severity":    r.severity,
                "enabled":     r.enabled,
                "weight":      r.weight,
            }
            for r in rules
        ]
        if rules
        else []
    )

    cards_html = _render_cards(rule_data, llm_data)

    # 规则总览数字
    rule_total_str = f"{len(rule_data)} 条 trace" if rule_data else ""
    llm_total_str  = f"{len(llm_data)} 条 trace" if llm_data else ""

    rule_section = ""
    if rule_data:
        stats_html    = _render_rule_stats_table(rule_data, all_rules)
        failures_html = _render_rule_failures(rule_data, all_rules)
        rule_section  = f"""
<div class="panel">
  <div class="panel-header">
    <span class="panel-title">规则评估器</span>
    <span class="panel-count">{rule_total_str}</span>
  </div>
  <div style="padding:12px 0 4px">
    <div style="padding:0 20px 8px;font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em">统计 — 按严重程度排序</div>
    {stats_html}
  </div>
  <div class="section-gap">
    <div style="padding:12px 20px 8px;font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em;border-top:1px solid #f1f5f9">错误明细</div>
    {failures_html}
  </div>
</div>"""

    llm_section = ""
    if llm_data:
        llm_inner  = _render_llm_evaluators(llm_data)
        llm_section = f"""
<div class="panel">
  <div class="panel-header">
    <span class="panel-title">LLM 评估器</span>
    <span class="panel-count">{llm_total_str}</span>
  </div>
  {llm_inner}
</div>"""

    # header meta
    sources = []
    if rule_path: sources.append(f"规则: {Path(rule_path).name}")
    if llm_path:  sources.append(f"LLM: {Path(llm_path).name}")

    meta_html = "".join(
        f'<span>{s}</span>' for s in ([f"生成于 {now}"] + sources)
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent 评测报告 · {now}</title>
<style>{_CSS}</style>
</head>
<body>

<div class="top-bar">
  <div class="top-bar-title">Agent 评测报告</div>
  <div class="top-bar-meta">{meta_html}</div>
</div>

<div class="page">
  {cards_html}
  {rule_section}
  {llm_section}
</div>

</body>
</html>"""


# ── 命令行入口 ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="生成 HTML 评测报告")
    parser.add_argument("--rule", default=None)
    parser.add_argument("--llm",  default=None)
    parser.add_argument("--out",  default=None)
    args = parser.parse_args()
    if not args.rule and not args.llm:
        parser.error("至少提供 --rule 或 --llm 之一")
    rule_data = load_rule_results(args.rule) if args.rule else None
    llm_data  = load_llm_results(args.llm)  if args.llm  else None
    report = render_html_report(rule_data, llm_data, args.rule or "", args.llm or "")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"HTML 报告 → {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
