#!/usr/bin/env python3
"""Analyze a chart image with the LLM and send a limit order to Telegram.

The flow the operator wants: LLM reads the chart, proposes a LIMIT order
(entry/stop/target + thesis), the discipline gate vets it, and it goes to
Telegram for the operator to place. Advisory — never executes.

Setup:
  export ANTHROPIC_API_KEY=sk-ant-...
  # in config.yaml, set TELEGRAM.enabled: true + token + chat_id
  #   (or set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars)

Usage:
  python scripts/chart_signal.py path/to/chart.png --instrument XAUUSD
  python scripts/chart_signal.py chart.png --context "H1, London session" --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.config_loader import load_config
from src.vision.chart_analyst import ChartAnalyst
from src.vision.chart_to_telegram import ChartToTelegram, format_limit_order


def main(argv) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("image", help="path to the chart image (png/jpg/webp)")
    p.add_argument("--instrument", default="XAUUSD")
    p.add_argument("--context", default="", help="extra context for the analyst")
    p.add_argument("--news", action="store_true", help="inside a tier-1 news window")
    p.add_argument("--dry-run", action="store_true", help="analyze + print, do not send to TG")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args(argv[1:])

    if not Path(args.image).exists():
        print(f"Image not found: {args.image}")
        return 1

    config = load_config(args.config)
    if not config["ANTHROPIC"].get("api_key"):
        print("No ANTHROPIC api key (set ANTHROPIC_API_KEY or config.yaml).")
        return 1

    import anthropic
    client = anthropic.Anthropic(api_key=config["ANTHROPIC"]["api_key"])
    analyst = ChartAnalyst(client=client)

    if args.dry_run:
        analysis = analyst.analyze_image(args.image, args.instrument, args.context)
        print(format_limit_order(analysis))
        print(f"\n[dry-run] setup_present={analysis.setup_present} "
              f"decision would gate on R:R={analysis.rr:.2f}")
        return 0

    from src.telegram_bot.bot import TelegramBot
    telegram = TelegramBot(config["TELEGRAM"])
    pipeline = ChartToTelegram(analyst=analyst, telegram=telegram)
    result = pipeline.run(args.image, args.instrument, args.context, in_news_window=args.news)

    print(result.message)
    print(f"\nDecision: {result.decision}   Sent to TG: {result.sent}")
    if result.reasons:
        for r in result.reasons:
            print(f"  - {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
