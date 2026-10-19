"""Self-contained, escaped HTML reports. No telemetry or external resources."""
from __future__ import annotations

from html import escape
from human_value.analysis import CATEGORIES


def render_html(report: dict) -> str:
    e = lambda value: escape(str(value), quote=True)
    messages = report["messages"]
    anchors = {m["id"]: f"message-{i}" for i, m in enumerate(messages)}

    def quote(ev):
        return (f'<blockquote>{e(ev["quote"])}<a href="#{anchors[ev["message_id"]]}">'
                f'{e(ev["message_id"])} ↗</a></blockquote>')

    turns = []
    for i, row in enumerate(report["turns"], 1):
        title = f'<div class="eyebrow">HUMAN TURN {i:02d} · {e(row["message_id"])}</div>'
        if row["status"] != "ok":
            turns.append(f'<article>{title}<h2>Analysis unavailable</h2><p>{e(row["error"])}</p><span class="tag">{e(row["status"])}</span></article>')
            continue
        a = row["analysis"]
        contributions = "".join(
            f'<div class="contribution"><span class="tag">{e(CATEGORIES[c["category"]])}</span>'
            f'<p>{e(c["description"])}</p>{quote(c["evidence"])}</div>'
            for c in a["contributions"]
        ) or '<p class="muted">No specific contribution identified in this message. This is not a judgment of the person.</p>'
        f = a["followup"]
        followup = f'<div class="followup"><div class="eyebrow">VISIBLE FOLLOWUP · {e(f["status"])}</div><p>{e(f["description"])}</p>{"".join(quote(v) for v in f["evidence"])}</div>'
        limitations = "".join(f'<li>{e(s)}</li>' for s in a["limitations"])
        turns.append(f'<article>{title}<h2>{e(a["summary"])}</h2>{contributions}{followup}<details><summary>Limits of this interpretation</summary><ul>{limitations}</ul></details></article>')
    transcript = "".join(
        f'<div class="message" id="{anchors[m["id"]]}"><span class="eyebrow">{e(m["role"].upper())} · {e(m["id"])}</span><p>{e(m["content"])}</p></div>'
        for m in messages
    )
    counts = report["summary"]["category_occurrences"]
    categories = "".join(f'<div class="category"><span>{e(label)}</span><b>{counts[key]}</b></div>' for key, label in CATEGORIES.items())
    fixture = report["provenance"] == "illustrative_fixture_not_model_output"
    banner = ("ILLUSTRATIVE DEMO · Synthetic conversation + authored annotations. No model was called."
              if fixture else "MODEL ANALYSIS · Quotes verified against the supplied text. Interpretations require human review.")
    limits = "".join(f'<li>{e(s)}</li>' for s in report["limits"])
    s = report["summary"]
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>Human Value Allocation — Contribution report</title>
<style>
:root{{--ink:#182a29;--muted:#526764;--paper:#f4f5ef;--line:#d8e0d6;--accent:#c7f073}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.65 system-ui,-apple-system,sans-serif}}header,main,footer{{max-width:1180px;margin:auto;padding:32px 32px}}header{{padding-top:48px}}nav{{display:flex;justify-content:space-between;align-items:center;gap:20px}}.brand{{font-weight:750;letter-spacing:-.5px}}.badge{{border:1px solid var(--ink);padding:4px 12px;border-radius:99px;font-size:12px;letter-spacing:1px}}h1{{font-size:clamp(38px,5.4vw,68px);line-height:1.08;letter-spacing:-2.5px;max-width:850px;margin:56px 0 24px}}.lead{{font-size:19px;max-width:750px;color:var(--muted)}}.banner{{background:#e7eddd;border-left:4px solid #709b37;padding:14px 18px;font-size:13px;margin-top:28px}}.layout{{display:grid;grid-template-columns:minmax(0,1fr) 295px;gap:32px}}article{{background:#fff;border:1px solid var(--line);padding:28px 30px;margin-bottom:24px;border-radius:12px}}h2{{font-size:23px;line-height:1.4;letter-spacing:-.4px;margin:12px 0 24px}}.eyebrow{{font:600 11px/1.5 ui-monospace,monospace;letter-spacing:1.2px;color:var(--muted);overflow-wrap:anywhere}}.tag{{display:inline-block;background:var(--accent);font-size:12px;font-weight:650;border-radius:5px;padding:4px 9px}}.contribution{{margin:22px 0}}.contribution p{{margin:8px 0}}blockquote{{border-left:2px solid #b6c6ad;margin:14px 0;padding:8px 14px;background:#f8faf5;font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}}a{{color:#476e2b}}blockquote a{{display:block;font:11px/1.5 ui-monospace,monospace;margin-top:8px}}.followup{{border-top:1px solid var(--line);padding-top:20px;margin-top:26px}}details{{margin-top:18px;font-size:13px;color:var(--muted)}}summary{{cursor:pointer}}aside .panel{{border:1px solid var(--line);border-radius:12px;padding:22px;margin-bottom:20px}}.stat{{font-size:44px;font-weight:700;line-height:1.3}}.category{{display:flex;justify-content:space-between;gap:10px;font-size:12px;padding:12px 0;border-bottom:1px solid var(--line)}}.category:last-child{{border:0}}.muted,.small{{color:var(--muted);font-size:13px}}.message{{border-bottom:1px solid var(--line);padding:22px 0;scroll-margin:20px}}.message p{{white-space:pre-wrap;overflow-wrap:anywhere}}.message:target{{background:#e7f2ce}}.transcript{{margin-top:40px}}footer{{border-top:1px solid var(--line);font-size:12px;color:var(--muted);overflow-wrap:anywhere}}@media(max-width:800px){{.layout{{grid-template-columns:1fr}}aside{{order:-1;display:grid;grid-template-columns:1fr 1fr;gap:12px}}header,main,footer{{padding:24px 18px}}h1{{margin-top:38px}}article{{padding:22px}}}}@media(max-width:480px){{aside{{display:block}}}}@media print{{body{{background:white}}.layout{{display:block}}article{{break-inside:avoid}}details{{display:block}}}}
</style></head><body>
<header><nav><div class="brand">Human Value Allocation</div><span class="badge">RESEARCH PREVIEW · v0.1</span></nav>
<h1>What did the human<br>bring to the work?</h1><p class="lead">Trace the information, corrections and decisions people contribute to AI conversations. Start with the evidence.</p><div class="banner">{e(banner)}</div></header>
<main><div class="layout"><section>{''.join(turns)}</section><aside>
<div class="panel"><div class="eyebrow">HUMAN TURNS ANALYZED</div><div class="stat">{s['analyzed_turns']} <span class="muted">/ {s['human_turns']}</span></div><p class="small">Unresolved: {s['unresolved_turns']}<br>Session: {e(report['session_id'])}</p></div>
<div class="panel"><div class="eyebrow">CONTRIBUTION OBSERVATIONS</div>{categories}<p class="small">Categories can overlap. These are counts, not value scores or percentages.</p></div>
<div class="panel"><div class="eyebrow">READ THIS REPORT AS</div><p class="small">A reviewable interpretation of visible dialogue. No causal, economic or token-equivalent value is estimated.</p></div>
</aside></div><section class="transcript"><div class="eyebrow">SOURCE CONVERSATION</div><h2>Every observation leads back to the text.</h2>{transcript}</section>
<section><h2>Scope & limitations</h2><ul>{limits}</ul><p class="small">Evaluation API attempts recorded: {len(report['evaluation_usage'])}. Usage belongs to the analyzer, not to the human.</p></section></main>
<footer>Human Value Allocation · MIT · {e(report['software_version'])}<br>Provenance: {e(report['provenance'])}<br>Requested model: {e(report.get('requested_model') or 'none — offline fixture')}<br>Evaluator SHA-256: {e(report['evaluator_sha256'])}</footer></body></html>'''
