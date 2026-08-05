# BLUEPRINT v2 — THE POTHOLE ADDENDUM
### Everything v1 left soft, now cemented

v1 gave the engine a mind (the prompt), a body (the architecture), and a memory row (the ledger schema). What it did **not** specify with enough rigor:

1. **What happens when the system drifts off its path** — v1 said "detectors trip and surface fixes." That's a dashboard, not self-healing. A system that *auto-finds its path* needs a formal state machine, checkpointed known-good states, and the ability to rebuild itself from first principles.
2. **Journaling as a memory architecture** — v1 gave one JSON row. You need *journals*: grouped, stacked, rolled up, cross-linked, and instantly findable years later.
3. **~14 quiet potholes** that don't show up in design docs but kill live systems.

This addendum cements all three. Everything here is implemented in the code that follows.

---

## PART VII — THE GUARDIAN: SELF-HEALING & PATH RE-FINDING

The core idea: **the engine must be able to rebuild its entire state from nothing but the immutable event log and a code version.** If that's true, no crash, drift, corruption, or bad deploy is unrecoverable — it can always walk back to a known-good point and re-derive itself forward. This is event sourcing applied to a trading brain, and it's what converts "hope it doesn't break" into "it cannot stay broken."

### VII.1 · The five health states

The engine is always in exactly one state. Transitions are logged, reasoned, and reversible.

| State | Meaning | Behaviour | Exit condition |
|---|---|---|---|
| **HEALTHY** | All detectors green, calibration in band | Full live publishing | detector trips → DEGRADED |
| **DEGRADED** | One or more non-critical detectors tripped | Publish at reduced conviction; widen gates; log loudly | detectors clear (N consecutive checks) → HEALTHY; critical trip → RECOVERING |
| **RECOVERING** | Critical fault: data gap, calibration collapse, state divergence | **Stop publishing.** Keep observing + journaling. Run repair: rollback to last known-good checkpoint, replay-rebuild, reconcile | verified state hash matches replay → DEGRADED (probation) |
| **SAFE** | Unrecoverable-by-self or risk breach (drawdown limit, feed dead, cost blowout) | Full stop on signals. Heartbeat + alert only. Human required | explicit operator clear |
| **REPLAY** | Offline/backfill mode | Deterministic reprocessing, no publishing, no Telegram | replay complete |

The critical design point: **RECOVERING is not "wait and hope."** It is an active repair procedure with a verification gate. The engine does not return to publishing until it can *prove* its rebuilt state matches a deterministic replay of the same inputs.

### VII.2 · How it actually re-finds its path (the repair ladder)

Escalating, each rung cheap→expensive. The engine climbs only as far as needed.

1. **Re-seat from hot state** — flush the forming bar, re-request the last N ticks, re-seal frames. Fixes transient feed hiccups.
2. **Rollback to last known-good checkpoint** — the Checkpoint Registry snapshots full engine state (all indicator accumulators, seat ratings, bandit params) every N minutes *and* every time health is verified HEALTHY. Rolling back is instant and safe because checkpoints are content-hashed and validated.
3. **Deterministic replay-rebuild** — discard in-memory state entirely; re-stream the immutable tick log from the last checkpoint forward through the *same code version*; recompute every FeatureFrame. Compare the resulting **state hash** against the live one. This is the ground-truth repair: it can *always* reconstruct correct state, because the log is immutable and the pipeline is deterministic.
4. **Parameter rollback** — if the fault is behavioural (calibration collapse, reward decay), revert the bandit's parameter policy and seat weights to the last snapshot that was *provably performing*, and re-enter probation.
5. **Roster rollback** — demote the current champion seat, restore the prior champion from the model registry, run challenger re-qualification.
6. **SAFE + escalate** — if verification still fails, stop and shout. Refusing to trade is always available and is never the wrong answer when the engine can't trust itself.

Each rung writes an **incident journal** entry: what tripped, which rung was climbed, what the verification showed, and whether it held. Over time this becomes the engine's immune memory — repeated fault signatures get faster, pre-diagnosed responses.

### VII.3 · Determinism is the enabler (non-negotiable)

Replay-rebuild only works if the pipeline is bit-deterministic. Therefore:

- **No wall-clock reads inside logic.** Time is an *input* (`t_event`), never `now()`. Every component receives time; none fetches it.
- **No unseeded randomness.** Every stochastic component (bandit exploration, challenger mutation) draws from a seeded, logged PRNG stream.
- **Point-in-time sealing.** A FeatureFrame is computed only from data with `t <= t_event`, then frozen and hashed. Late-arriving ticks *never* mutate a sealed frame — they open a new one, flagged.
- **Code version pinned in every record.** `code_version` + `feature_schema_version` are stamped on every frame, signal, and checkpoint. The learner refuses to mix versions; replay refuses to verify across a version boundary.
- **Idempotent writes.** Every record is content-addressed; replaying the same input twice produces the same ID and the second write is a no-op. Recovery can therefore be run repeatedly without duplicating history.

### VII.4 · Repaint safety (the silent killer v1 didn't name)

Swing pivots, divergences, and structure breaks are the heart of the strategy — and they are exactly the features that **repaint**. A pivot "exists" only after `k` bars confirm it. If you let a strategist see an unconfirmed pivot, you have leaked the future into the past and every backtest becomes a beautiful lie.

Cemented rule: **pivots are emitted only after `k`-bar confirmation, and carry both `t_pivot` (when it happened) and `t_confirmed` (when it became knowable). All downstream logic keys off `t_confirmed`.** Divergence detection uses only confirmed pivots. Forming bars are visible as `live` context but can never trigger a structural event. This one rule is worth more than any model improvement.

---

## PART VIII — THE JOURNAL: A MEMORY ARCHITECTURE, NOT A LOG FILE

You said it twice, so it's load-bearing: **whole journals, arranged and grouped and stacked smartly, easily findable and referenceable.** A flat JSONL of a million rows is not memory — it's a landfill. Memory needs strata, structure, and retrieval.

### VIII.1 · Six strata (each answers a different question, at a different timescale)

| # | Stratum | Answers | Cadence | Format |
|---|---|---|---|---|
| 0 | **Event log** | "What literally happened, tick by tick?" | continuous | JSONL + WAL, machine |
| 1 | **Decision journals** | "Why did we fire *this* signal, and what happened?" | per signal | JSON + Markdown narrative |
| 2 | **Session diary** | "What happened today, and what did we learn?" | per session (daily) | Markdown + JSON summary |
| 3 | **Retrospectives** | "What changed this week/month? Is it improving?" | weekly / monthly | Markdown, rolled up |
| 4 | **Lesson library** | "What do we *know* now that we didn't before?" | continuous, distilled | Markdown per lesson, deduplicated, versioned |
| 5 | **Incident journal** | "What broke, why, how did it heal, did it hold?" | per incident | Markdown + structured cause/fix |

Stratum 4 is the crown jewel. Lessons are **distilled, deduplicated, and reinforced**: when the same pattern is confirmed again, the lesson's `confidence` and `evidence_count` rise and the new signal is backlinked to it. When contradicted, the lesson is *challenged* and either narrowed (regime-scoped) or retired — with the retirement reasoned and preserved. That is institutional memory that compounds.

### VIII.2 · How they're arranged (findable in three years, not three days)

```
journal/
├── INDEX.db                        # SQLite + FTS5: full-text over every journal
├── MANIFEST.json                   # top-level map: what exists, where, integrity hashes
├── lessons/
│   ├── LESSON_INDEX.md             # human-browsable, grouped by theme
│   └── absorption_at_highs/
│       ├── lesson.md               # the distilled knowledge, versioned
│       └── evidence.jsonl          # every signal_id that supports/contradicts it
├── incidents/
│   └── 2026/08/INC-20260801-0001-feed_gap/…
└── sessions/
    └── 2026/08/01/
        ├── SESSION.md              # the day's diary (human read)
        ├── SESSION.json            # the day's stats (machine read)
        ├── MANIFEST.json           # everything in this day + hashes
        ├── signals/
        │   ├── SIG-20260801-0007-B-short.md    # narrative: the full thought process
        │   └── SIG-20260801-0007-B-short.json  # structured ledger row
        ├── standdowns.jsonl        # what it chose NOT to do, and why (training data!)
        └── events.jsonl            # raw stratum-0 for the day
```

**Naming law:** every artifact ID is sortable, self-describing, and stable — `SIG-{YYYYMMDD}-{seq}-{seat}-{direction}`. You can guess a path from an ID and grep a date range without an index. The index makes it instant; the naming makes it survivable *without* the index.

**Backlinks are bidirectional.** A signal links to its frame, its seat, its lessons, and its outcome. A lesson links back to every signal that formed it. An incident links to every signal emitted during the degraded window (so you can quarantine tainted training data). You can walk the graph from any node in either direction — that's what "easily referenced" actually requires.

**Rollup & retention.** Raw ticks: hot 7d → warm 90d (compressed) → cold archive (Parquet). Journals: **never deleted**, compressed after 90d. Every rollup writes a manifest with hashes so integrity is verifiable years later.

### VIII.3 · The narrative journal (your literal requirement)

Every signal gets a Markdown file written in plain English — the strategist's actual argument, the predicted path, the counter-case it argued against itself, and later, an appended **post-mortem** section: what really happened, whether the mechanism held, what was learned. You can open any signal from any day and read the machine's mind at that moment. That is the artifact that makes this system trustworthy rather than magic.

---

## PART IX — THE REMAINING POTHOLES (each named, each cemented)

**1 · Signals must be net of cost.** Spread, commission, and slippage are modeled at emission. A 3-tick target on gold with a 3-tick spread is not a trade, it's a donation. Cemented: every signal computes `edge_after_cost`; if TP1 doesn't clear cost by a required multiple, it is not emitted. The learner is trained on *net* R, never gross — otherwise it learns fantasy.

**2 · Correlated signals are one bet, not five.** Five longs at the same level in the same hour is a single leveraged position wearing five hats. Cemented: the Arbiter computes signal correlation (direction × level proximity × time proximity) and caps *aggregate* exposure, publishing the strongest and marking the rest as confluence rather than new entries.

**3 · Exploration budget.** A pure exploit-loop never discovers a better stop placement — it converges on a local optimum and calls it wisdom. Cemented: a fixed, small fraction of signals (e.g. 5–10%) are deliberately explored — perturbed parameters, flagged `is_exploration=true`, excluded from headline stats but included in learning. Without this, the meta-learner plateaus and looks fine while doing so.

**4 · Cold start.** Day 1 has no data and no rankings. Cemented: bootstrap by **historical replay** — run the identical pipeline over months of archived ticks in REPLAY state, producing a real prior ledger before a single live signal. Because the pipeline is deterministic and point-in-time sealed, replay results are legitimate training data, not a backtest fantasy.

**5 · Non-stationarity.** Gold in 2023 ≠ gold now. Cemented: recency-weighted training (exponential decay on sample weight), regime-conditioned evaluation, and rolling-window revalidation. Any lesson that only held in a dead regime gets regime-scoped rather than deleted.

**6 · Multiple-testing / p-hacking.** Run 50 challenger lenses and one will look brilliant by luck. Cemented: challengers must clear a significance bar adjusted for the number tested (Benjamini–Hochberg style FDR control), *and* survive an out-of-sample shadow window before promotion.

**7 · Clock skew & DST.** Broker time drifts; session anchors move with New York DST. Cemented: all internal time is UTC epoch-nanos, session boundaries derived from `America/New_York` 17:00 via `zoneinfo` (never a hardcoded UTC hour), and broker-vs-local skew is measured and logged continuously.

**8 · Data revision.** Brokers correct bad prints. Cemented: revisions arrive as new events referencing the original; sealed frames are never mutated — instead the affected window is marked `tainted` and any signals born there are flagged for the learner to down-weight.

**9 · Backpressure & latency budget.** At 1 Hz × N seats, an LLM queue can silently fall behind and start reasoning about stale tape. Cemented: every frame carries a deadline; a proposal arriving after its frame's deadline is *rejected, not published*, and the drop is counted. Latency is a first-class metric with its own detector.

**10 · Cost governor.** Cemented: a hard daily spend ceiling with graceful degradation — pre-filter thresholds tighten automatically as budget depletes, so the engine gets *pickier* rather than going dark.

**11 · Stand-downs and near-misses are data.** v1 said log them; cementing the *why*: they're the negative class. Without them the learner only ever sees the cases where it acted, which is textbook selection bias. Every stand-down logs the pre-filter score and the reason it didn't clear.

**12 · Path-match needs a real algorithm.** "Did price take the predicted route" was hand-waved. Cemented: predicted paths are parsed into a canonical **event sequence** (sweep → reject → reclaim → trend), the realized tape is parsed into the same vocabulary, and the two are scored by normalized edit-distance / DTW. Now `path_match` is a real number the learner can optimize.

**13 · Secrets & PII hygiene.** Journals are permanent and human-readable. Cemented: API keys and account identifiers never enter a journal; a redaction pass runs on write.

**14 · The "5/day" honesty rail.** Restating with teeth: the target is measured as a *rolling 7-day mean*, never a daily quota, and undershoot triggers a **diagnosis**, not a loosening of the quality bar. The only sanctioned response is: tune pre-filter *sensitivity*, add a regime-appropriate lens, or accept and log a genuinely thin market.

---

## WHAT THIS MEANS FOR THE BUILD

The foundation code that follows implements, from the ground up:

- **Determinism-first core** — time as input, seeded randomness, content-addressed immutable records, state hashing
- **No-repaint feature engine** — confirmed-pivot discipline baked into the types, not bolted on
- **Crash-safe ledger** — length-prefixed CRC-checked WAL with torn-write recovery
- **The six-stratum journal** with SQLite+FTS index, manifests, backlinks, and lesson distillation
- **The Guardian** — health state machine, detectors, checkpoint registry, and replay-rebuild verification
- **Learning primitives** — calibration (Brier/reliability/isotonic), calibration-weighted Elo, DTW path-match, cost-net reward, contextual bandit

These are the raw blocks. Everything else in the system is assembled from them.
