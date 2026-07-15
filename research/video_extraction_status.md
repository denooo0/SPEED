# Video Extraction — Capability Status & Autopilot Plan

## Can ATLAS hunt videos on autopilot?

**Short answer:** the pipeline is built for it, but THIS remote build
environment's network policy blocks YouTube, so extraction must run where
YouTube is reachable.

## What was tested (2026-07)

| Method | Result |
|---|---|
| WebFetch on youtube.com | **403 Forbidden** (YouTube blocks server fetches) |
| WebSearch | Session rate-limit hit (temporary) |
| `yt-dlp` via the sandbox proxy | **403** — gateway policy denies `www.youtube.com:443` |
| Proxy status check | Confirmed: `connect_rejected` policy denial for youtube.com |

The block is the remote environment's **network policy**, not a missing
capability. See https://code.claude.com/docs/en/claude-code-on-the-web for
how environment network policies are configured.

## Three ways to unblock autopilot extraction

1. **Operator runs the extractor locally (fastest today).**
   ```
   pip install youtube-transcript-api yt-dlp
   python scripts/extract_transcripts.py --from-file research/video_queue.txt
   ```
   Then commit `research/transcripts/*.md`. ATLAS reads those offline and
   extracts mechanisms — no YouTube access needed for the analysis step.

2. **Change the environment's network policy** to allow youtube.com, then a
   build session can run `extract_transcripts.py` itself. This makes it fully
   autopilot inside ATLAS.

3. **Text-first research (works now, once WebSearch resets).** For concepts
   like PO3 that are documented in articles/forums, ATLAS can WebSearch +
   WebFetch text sources directly — no video needed. Already done for PO3:
   see `research/po3_mechanism.md`.

## The autopilot pipeline (once transcripts exist)

```
video_queue.txt
     │  scripts/extract_transcripts.py  (runs where YouTube is reachable)
     ▼
research/transcripts/*.md          ← clean, timestamp-stripped text
     │  ATLAS reads offline
     ▼
mechanism extraction               ← strip hype, keep falsifiable claims
     │
     ▼
hypothesis specs                   ← research/hypotheses/*.yml
     │  BacktestRunner.evaluate_hypothesis
     ▼
SHELVED  or  SHADOW (paper)  →  LIVE
```

The filtration the operator asked for — "extract the mechanism, ignore the
hype, test what's testable" — is exactly this pipeline. The only manual step
today is getting the transcripts across the YouTube network boundary.

## Queue status

- `research/video_queue.txt` — 11 operator-supplied videos + 1 playlist,
  ready for `extract_transcripts.py`.
- `research/po3_mechanism.md` — PO3 already extracted from public framework
  knowledge (didn't need the videos to begin).

## Honest note on YouTube trading content

Most is recycled. The extractor pulls text; the VALUE step is ATLAS
filtering it: a claim only survives if it becomes a falsifiable hypothesis
that clears the backtest gate. A charismatic 40-minute video that reduces to
"buy support, sell resistance" produces zero hypotheses and is correctly
discarded. The signal is in what survives the gate, not in the view count.
