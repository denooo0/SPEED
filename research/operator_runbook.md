# Operator Runbook — Network, Transcripts, Backtest

Three things you asked about, answered concretely.

---

## 1. Why `python scripts/extract_transcripts.py` failed on your machine

Your terminal showed:
```
C:\Users\bt2\Documents\ATLAS>python scripts/extract_transcripts.py --from-file research/video_queue.txt
python: can't open file 'scripts/extract_transcripts.py': [Errno 2] No such file or directory
```

**Cause:** the `ATLAS` folder you were in did not contain the repo. You
installed the pip packages fine, but the ATLAS code wasn't there. You need to
clone the repo first, then run from inside it.

**Fix (Windows cmd):**
```cmd
cd C:\Users\bt2\Documents
git clone <your-repo-url> ATLAS-SPEED
cd ATLAS-SPEED
git checkout claude/xauusd-trading-bot-S5oPS

pip install youtube-transcript-api yt-dlp
python scripts\extract_transcripts.py --from-file research\video_queue.txt
```
(Use backslashes `\` in Windows paths. Confirm the file exists first with
`dir scripts\extract_transcripts.py`.)

The transcripts land in `research\transcripts\*.md`. Commit them and push; then
ATLAS reads them offline and extracts mechanisms — no YouTube access needed for
the analysis step.

**Whole playlist in one shot:**
```cmd
python scripts\extract_transcripts.py --playlist "https://www.youtube.com/playlist?list=PLPlvZjthhP6ke1WC8-p2HGZlRDFJKa6NY"
```

---

## 2. How to let the ATLAS build environment reach YouTube (and data hosts)

The ATLAS remote build sandbox runs behind a **network policy** chosen when the
environment was created. Right now that policy blocks youtube.com AND the data
hosts (api.bybit.com, yfinance, stooq) — every one returns 403 at the gateway.

**To change it:** the network policy is an environment-level setting, not
something code can flip from inside the sandbox. Configure it where you manage
the environment:

- Open the ATLAS environment settings (the place you created the Claude Code
  web/remote environment).
- Change the **network policy** from the restricted default to one that permits
  outbound to the hosts you need. The available policies are documented at
  https://code.claude.com/docs/en/claude-code-on-the-web — the section on
  network policies / egress.
- Hosts to allow for full autonomy:
  - `www.youtube.com`, `youtube.com`, `*.googlevideo.com`  (transcripts)
  - `api.bybit.com`  (historical candles)
  - `*.stlouisfed.org` / `api.stlouisfed.org`  (FRED macro)
  - `www.cftc.gov`  (COT archive)
  - `query1.finance.yahoo.com` / `query2.finance.yahoo.com`  (yfinance fallback)

Once the policy allows those, a build session can run `download_history.py` and
`extract_transcripts.py` itself — fully autopilot, no manual steps.

**If you'd rather not widen the policy:** run those two scripts on your own
machine (which has open internet), commit the outputs (`data/*.parquet`,
`research/transcripts/*.md`), and push. ATLAS does all the analysis offline.
Both paths reach the same place; the policy change just removes the manual hop.

---

## 3. Running the backtest (data required)

The engine is **certified clean** — `scripts/audit_lookahead.py` poisons every
future bar with 8–100x garbage and proves no past feature or past decision
changes (see `research/engine_integrity.md`). That audit also runs in the test
suite (`tests/test_lookahead_audit.py`), so it can never silently regress.

To run the real edge test:

```bash
# 1. Get data (on a machine/policy with internet)
pip install -r requirements.txt
python scripts/download_history.py --start 2019-01-01 --timeframes 5m 15m 1h --macro --cot
#    If Bybit's XAUUSD symbol is empty, try --symbol XAUTUSDT,
#    or export candles from MT5 and use:
#    python scripts/download_history.py --from-csv gold_5m.csv --csv-timeframe 5m

# 2. Run the backtest of your demonstrated edge
python scripts/run_backtest.py --setup htf-retracement-continuation --timeframe 5m
```

The verdict prints Sharpe, DSR, PBO, walk-forward efficiency, and OOS trade
count against the promotion gate (PBO < 0.20, DSR > 0.95, WFE ≥ 0.5 on ≥ 70%
folds, ≥ 200 OOS trades). PASS → promote to paper (SHADOW). FAIL → the raw
pattern lacks edge at those params; parameter-search (logged as trials so DSR
stays honest) or shelve it.

**Without data**, `run_backtest.py` runs a clearly-stamped SYNTHETIC dry-run
that proves the pipeline works end-to-end but is NOT evidence of edge.

---

## The honest bottleneck

Everything is built and certified. The single thing between us and the first
real answer — *does "sell the retracement into the declining MA in a downtrend"
survive realistic costs on years of gold?* — is **getting historical data past
the network boundary.** That's a 5-minute data download on any open-internet
machine, or a network-policy change. Nothing else blocks the edge test.
