# Inbox — drop your data here (no local dev needed)

This folder is the friction-free way to get data to ATLAS. You do NOT need to
clone the repo, install Python, or run anything on your machine.

## The 3-step path (all in your browser + MetaTrader)

### Step 1 — Export gold history from MetaTrader

In MT5:
1. Open the **XAUUSD** chart, pick a timeframe (M5, M15, or H1).
2. Scroll back / press **Home** and keep pressing **Page Up** to load lots of
   history (the more bars, the better — aim for years).
3. **Tools → History Center** (or right-click chart → **Save As / Export**),
   or: **File → Save As** and choose CSV.
   - MT5 export format is fine as-is (tab-separated, `<DATE> <TIME> <OPEN> …`).
   - ATLAS auto-detects this format.
4. Name the file with the timeframe so ATLAS knows it, e.g.:
   - `XAUUSD_M5.csv`
   - `XAUUSD_M15.csv`
   - `XAUUSD_H1.csv`

### Step 2 — Upload it here via the GitHub website

1. Go to the repo on **github.com** → open this `inbox/` folder.
2. Click **Add file → Upload files**.
3. Drag your `XAUUSD_*.csv` file(s) in.
4. Click **Commit changes**.

That's it. No terminal, no git, no Python on your side.

### Step 3 — Tell ATLAS "data's in the inbox"

In the ATLAS chat, just say: **"data's in the inbox, run the backtest."**

ATLAS will:
1. `git pull` your uploaded CSV into the sandbox.
2. Ingest it (`download_history.py --inbox`) → `data/*.parquet`.
3. Run the backtest of your demonstrated edge and report the honest verdict
   (Sharpe / DSR / PBO / walk-forward / trades against the promotion gate).

## Filename → timeframe mapping

ATLAS infers the timeframe from the filename token:

| Filename contains | Timeframe |
|---|---|
| `M1` / `1m` | 1m |
| `M5` / `5m` | 5m |
| `M15` / `15m` | 15m |
| `M30` / `30m` | 30m |
| `H1` / `1h` | 1h |
| `H4` / `4h` | 4h |
| `D1` / `daily` | 1d |

Default if none found: 5m. Symbol defaults to XAUUSD.

## Notes

- Uploaded CSVs are gitignored from the permanent history (they can be large),
  but they persist long enough for ATLAS to ingest them into parquet.
- More history = more trades = a more trustworthy backtest. The promotion gate
  wants ≥ 200 out-of-sample trades, so export as far back as MT5 lets you.
- If you have multiple timeframes, upload all of them — the feature engine uses
  M5 as the base and can align higher timeframes.
