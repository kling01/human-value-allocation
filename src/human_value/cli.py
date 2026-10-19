"""Command line interface for local reports and explicit remote evaluation."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from human_value.analysis import analyze, demo_report
from human_value.render import render_html


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyze human contributions with source evidence.")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Render an authored example offline; no API or prediction.")
    run = commands.add_parser("analyze", help="Send a dialogue to your configured model and produce a local report.")
    run.add_argument("input", type=Path)
    run.add_argument("--model", default=os.getenv("HVA_MODEL", ""))
    run.add_argument("--base-url", default=os.getenv("HVA_BASE_URL", ""))
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--allow-insecure-http", action="store_true", help="Explicitly permit a remote HTTP gateway; HTTPS is the default requirement.")
    for command in (demo, run):
        command.add_argument("--out", type=Path, required=True, help="New output directory (must not already exist).")
    args = parser.parse_args(argv)
    try:
        # Refuse before starting any billable work.
        if args.out.exists():
            raise ValueError("Output directory already exists; choose a new one to preserve prior results.")
        if args.command == "demo":
            report = demo_report()
        else:
            if args.input.stat().st_size > 2_000_000:
                raise ValueError("Input file exceeds the 2 MB preview limit.")
            data = json.loads(args.input.read_text(encoding="utf-8"))
            report = asyncio.run(analyze(data, api_key=os.getenv("HVA_API_KEY", ""),
                                        base_url=args.base_url, model=args.model, concurrency=args.concurrency,
                                        allow_insecure_http=args.allow_insecure_http))
        html = render_html(report)
        args.out.mkdir(parents=True, exist_ok=False)
        (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / "report.html").write_text(html, encoding="utf-8")
        print(f"Report: {args.out / 'report.html'}")
        print(f"Data:   {args.out / 'report.json'}")
        if report["summary"]["unresolved_turns"]:
            print("Some turns are unresolved. They are marked explicitly in the report.", file=sys.stderr)
            return 2
        return 0
    except (ValueError, OSError) as exc:
        # Do not echo arbitrary exception text from providers/configuration.
        if isinstance(exc, OSError):
            message = "Could not read input or write output. Check paths and permissions."
        elif isinstance(exc, json.JSONDecodeError):
            message = "Input must be valid JSON."
        else:
            message = str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
