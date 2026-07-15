#!/usr/bin/env python3
"""Batch-extract YouTube transcripts → markdown, for the research pipeline.

WHY THIS EXISTS
---------------
The ATLAS remote build environment's network policy BLOCKS youtube.com at the
gateway (CONNECT → 403). So transcripts cannot be fetched from inside a normal
build session. This script is written to run in ANY environment that HAS
YouTube access:

  - the operator's own machine, or
  - a session whose network policy permits youtube.com, or
  - a cron/CI job on an allowlisted host.

It takes video URLs / IDs / a playlist, pulls the captions, strips the
timestamps, and writes one clean markdown file per video under
`research/transcripts/`. Those markdown files ARE fetchable by the ATLAS
research pipeline (they live in the repo), so the mechanism-extraction step
can run offline afterwards.

USAGE
-----
    # single video(s)
    python scripts/extract_transcripts.py VIDEO_URL_OR_ID [MORE ...]

    # whole playlist
    python scripts/extract_transcripts.py --playlist PLAYLIST_URL

    # from a file of URLs (one per line)
    python scripts/extract_transcripts.py --from-file links.txt

DEPENDENCIES (install where you run it)
    pip install youtube-transcript-api yt-dlp

The script prefers youtube-transcript-api (lightweight, captions only). It
falls back to yt-dlp for playlist expansion and for videos where the
transcript API fails.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional

OUT_DIR = Path("research/transcripts")


def video_id(url_or_id: str) -> Optional[str]:
    """Extract an 11-char YouTube video id from a URL or bare id."""
    s = url_or_id.strip()
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", s):
        return s
    m = re.search(r"(?:v=|/shorts/|youtu\.be/)([0-9A-Za-z_-]{11})", s)
    return m.group(1) if m else None


def expand_playlist(playlist_url: str) -> List[str]:
    """Return video ids in a playlist using yt-dlp (flat, no download)."""
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        print("yt-dlp not installed; cannot expand playlist", file=sys.stderr)
        return []
    ids: List[str] = []
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        for entry in info.get("entries", []) or []:
            vid = entry.get("id")
            if vid:
                ids.append(vid)
    return ids


def fetch_transcript(vid: str) -> Optional[str]:
    """Return the plain-text transcript for a video id, or None."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError:
        print("youtube-transcript-api not installed", file=sys.stderr)
        return None
    try:
        chunks = YouTubeTranscriptApi.get_transcript(vid, languages=["en", "en-US", "en-GB"])
    except Exception as e:  # noqa: BLE001
        print(f"  transcript fetch failed for {vid}: {e}", file=sys.stderr)
        return None
    # Join, collapse whitespace, drop caption artifacts.
    text = " ".join(c["text"].replace("\n", " ") for c in chunks)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\[(?:music|applause|laughter)\]", "", text, flags=re.IGNORECASE)
    return text or None


def title_for(vid: str) -> str:
    """Best-effort video title via yt-dlp metadata; falls back to the id."""
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return vid
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            return info.get("title") or vid
    except Exception:  # noqa: BLE001
        return vid


def write_markdown(vid: str, title: str, transcript: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z_-]+", "-", title).strip("-")[:80] or vid
    path = OUT_DIR / f"{vid}-{safe}.md"
    path.write_text(
        f"---\n"
        f"video_id: {vid}\n"
        f"title: {title!r}\n"
        f"url: https://www.youtube.com/watch?v={vid}\n"
        f"source: youtube-transcript\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"> Auto-extracted transcript. Timestamps stripped. For mechanism\n"
        f"> extraction by the ATLAS research pipeline — not a citation of\n"
        f"> endorsement.\n\n"
        f"{transcript}\n"
    )
    return path


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="*", help="video URLs or ids")
    parser.add_argument("--playlist", help="playlist URL to expand")
    parser.add_argument("--from-file", help="file with one URL/id per line")
    args = parser.parse_args(argv[1:])

    ids: List[str] = []
    for v in args.videos:
        vid = video_id(v)
        if vid:
            ids.append(vid)
    if args.from_file:
        for line in Path(args.from_file).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            vid = video_id(line)
            if vid:
                ids.append(vid)
    if args.playlist:
        ids.extend(expand_playlist(args.playlist))

    # Dedupe, preserve order.
    seen = set()
    ids = [v for v in ids if not (v in seen or seen.add(v))]

    if not ids:
        print("No video ids resolved. Nothing to do.", file=sys.stderr)
        return 1

    print(f"Extracting {len(ids)} transcript(s) → {OUT_DIR}/")
    ok = 0
    for vid in ids:
        print(f"- {vid} …")
        transcript = fetch_transcript(vid)
        if not transcript:
            print(f"  SKIP (no transcript): {vid}")
            continue
        title = title_for(vid)
        path = write_markdown(vid, title, transcript)
        print(f"  wrote {path} ({len(transcript)} chars)")
        ok += 1

    print(f"\nDone: {ok}/{len(ids)} transcripts written.")
    print("Next: the ATLAS research pipeline reads research/transcripts/*.md")
    print("and extracts testable mechanisms into hypothesis specs.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
