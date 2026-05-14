#!/usr/bin/env python3
"""Render a SITUATION REPORT JSON to a sanitized X post (stdout).

Usage:
    python scripts/post_to_x.py path/to/situation_report.json
    python scripts/post_to_x.py - < situation_report.json

The script does NOT call X / Twitter APIs — it just prints the rendered
post(s). Pipe to your posting tool of choice, or paste manually.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.llm.schema import SituationReport
from src.publishing.x_post import PostConfig, render_post


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return 1
    src = argv[1]
    raw = sys.stdin.read() if src == "-" else Path(src).read_text()
    sr = SituationReport.model_validate_json(raw)
    cfg = PostConfig()
    posts = render_post(sr, cfg)
    for i, post in enumerate(posts, start=1):
        if len(posts) > 1:
            print(f"--- post {i}/{len(posts)} ({len(post)} chars) ---")
        else:
            print(f"--- ({len(post)} chars) ---")
        print(post)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
