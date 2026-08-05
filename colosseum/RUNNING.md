# RUNNING — START_HERE, as actual commands

`START_HERE.md` is the plan. This is the wiring: the same five steps, each one
command, plus an honest statement of what is blocked and on what.

Colosseum is a **self-contained project** that happens to live in this repo. It
does not import from `src/`, and `src/` does not import from it. Keep it that
way unless you deliberately decide otherwise.

---

## Status

| Step | What it does | State |
|---|---|---|
| 0 | Prove the code runs | **done — 124/124 green** |
| 1 | Telegram capture + backfill | **blocked: needs your `my.telegram.org` credentials** |
| 2 | Get gold bars | **blocked: needs a data source you have access to** |
| 3 | Are the channels profitable? | **ready — `cli verdict`** |
| 4 | Recover the rulebook | **ready — `cli rulebook`** |
| 5 | Forced flow | **ready — `cli forcedflow`** |

Steps 3–5 are written, wired and tested end-to-end against synthetic data. They
run the moment Steps 1 and 2 hand them real inputs. Nothing about them is a
stub.

---

## Step 0 — prove it runs

```bash
python -m colosseum.cli selftest      # 124/124, standard library only
```

No dependencies needed. Every third-party import in the package is lazy, so the
engine and its whole verification suite run on a bare Python 3.11+.

## Step 1 — Telegram capture (yours to run)

```bash
pip install telethon
cp colosseum/.env.example .env        # then fill in api_id / api_hash
set -a && . ./.env && set +a

python -m colosseum.mirror.telegram_ingest --backfill --months 6
python -m colosseum.mirror.telegram_ingest --summary
nohup python -m colosseum.mirror.telegram_ingest --live &
```

Only you can do this: the first run sends a login code to your phone.

> **The `.session` file is a credential.** Whoever holds it holds your Telegram
> account — no password, no 2FA prompt. It is gitignored. Keep it that way.

Start live capture **today**, before you have bars, before you've decided
anything. Backfill recovers history; only live capture records what gets
*deleted*, and `deletions.jsonl` is the only place survivorship bias ever
becomes visible.

## Step 2 — bars (yours to get)

Any CSV with `time,open,high,low,close,volume` works. Column names are matched
loosely (Dukascopy, MT4/MT5, Polygon, Twelve Data and TradingView exports all
load as-is), and Parquet works if `pyarrow` is installed.

1-minute bars for Steps 3–4. 15-minute, 1–2 years, for Step 5.

> **Check `--tz` before you trust any output.** If your broker exports UTC+2
> server time, pass `--tz 2`. Get this wrong and every signal lands in the wrong
> trading session, which silently invalidates Step 5 and skews Step 4.

## Step 3 — the question that decides everything

```bash
python -m colosseum.cli verdict \
    --archive mirror_data/raw/*.jsonl \
    --bars gold_1m.csv --tz 0 --json
```

Reports the parse rate first, on purpose. **If the parser understood 40% of
messages, the verdict describes that 40%, not the channel.** It also tells you
how many signals fell outside your bar coverage rather than quietly dropping
them.

Three outcomes, and the tool prints which one you got:

- **PROFITABLE** → Step 4.
- **MARGINAL** → collect more data. Do not talk yourself into it.
- **NOT PROFITABLE** → stop. That is a successful result. You just saved months.

## Step 4 — recover the rulebook

```bash
python -m colosseum.cli rulebook \
    --archive mirror_data/raw/channel_one.jsonl \
    --bars gold_1m.csv --json
```

**This refuses to run unless Step 3 wrote a PROFITABLE verdict for that
channel.** That guard is the point — mining the rules of a losing channel is how
you spend a month cloning a loss. `--force` overrides it if you want the
negative example.

Every anchor it uses is computed **point-in-time**, from bars that had already
closed when the signal was posted. Swings must be confirmed by bars on both
sides. A swing that only became a swing an hour later is hindsight, and letting
one in manufactures a rule that never existed.

Read `AMBIGUOUS` before you believe anything. Where two anchors explain the
levels equally well, the tool says so instead of picking one — do not pick one
either.

## Step 5 — forced flow (run this now, it needs no Telegram)

```bash
python -m colosseum.cli forcedflow --bars gold_15m.csv --json
```

Independent of MIRROR entirely, so it is not blocked on your Telegram
credentials — only on bars.

**The volatility multipliers are the immediate payoff.** Feed them into stop and
target sizing right away. A stop sized for the London/NY overlap is far too
tight for Asia, and that one correction usually beats any new signal logic.

Treat directional findings with suspicion even when they pass. They are held to
FDR correction *and* a sign-stability test across halves precisely because that
is where seasonal folklore comes from. A window marked UNSTABLE is a fitted
artifact.

---

## What NOT to do

- **Don't screenshot.** MTProto gives structured data with exact timestamps and
  edit history. Screenshots throw all of it away.
- **Don't use a bot.** Bots cannot read history from before they joined, which
  makes the 6-month backfill impossible.
- **Don't skip Step 3.**
- **Don't trust a claimed win rate.** Measure it against your own tape.
- **Don't loosen the statistical gates when they reject things.** They are
  calibrated to reject things. That is the job.

---

## New in this repo (not in the original package)

- `colosseum/bars.py` — loads CSV/Parquet bars; derives point-in-time swings,
  VWAP + bands, session levels, and a volume profile (VPOC / value area) for
  Step 4. Anything it cannot compute honestly is left `None`, which the miner
  treats as "this anchor does not exist" — missing beats guessed.
- `colosseum/steps.py` — the Step 3/4/5 drivers behind the three CLI commands.
- `colosseum/.env.example`, `colosseum/requirements.txt`.
