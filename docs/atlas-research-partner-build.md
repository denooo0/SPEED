# Build: ATLAS Research Partner + Hypothesis Forge

> What was built, why, where it lives, and how to use it.
> Branch: `claude/atlas-research-partner-skill-O2i3U`.

---

## 1. What this is

A research-mode capability for ATLAS, in two layers plus the first concrete
pipeline stage it governs:

1. **The doctrine** — `prompts/ATLAS_RESEARCH_PARTNER.md`. Layered onto the trading
   mandate. The mandate governs when ATLAS *trades*; this governs how ATLAS
   *discovers what is worth trading*. Default verdict: **"noise / overfit until the
   backtest oracle says otherwise."**
2. **The skill** — `.claude/skills/atlas-research-partner/SKILL.md`. A Claude Code
   agent skill that loads the doctrine, maps a request onto the edge-generation
   pipeline, runs the forge → falsify → validate loop, and dispatches specialist
   skills (`verify`, `run`, `code-review`, `claude-api`, `security-review`, `loop`)
   at the right gates. `promo amplifier` is referenced as the future Phase-4 handoff.
3. **The forge stage** — `src/research/`. The front door of the pipeline that the
   skill orchestrates: turns a raw observation into one falsifiable, backtest-ready
   `Hypothesis`. This closes the previously-dangling references the skill pointed at.

---

## 2. Artifacts

| File | Purpose |
|---|---|
| `prompts/ATLAS_RESEARCH_PARTNER.md` | Research-mode doctrine: who ATLAS is at the workbench, the prime directive, the research surface (four lenses + distant domains), the workflow, ten laws of research, how it thinks, what it never does, the skill-handoff rules, tone, purpose. |
| `.claude/skills/atlas-research-partner/SKILL.md` | The agent skill. Step 0 loads the doctrine; steps 1–4 locate the work, run the core loop, dispatch specialist skills, and compress the result. Hard guardrails at the end. |
| `prompts/ATLAS_FORGE.md` | System prompt for the forge LLM call. The forge role + the three things every hypothesis must have + the laws obeyed at the door. |
| `src/research/__init__.py` | Package exports. |
| `src/research/hypothesis.py` | Pydantic `Hypothesis` schema (+ `EntryRule`, `ExitRule`, `Filters`, `ExpectedEdge`) mirroring `plan/edge_generation_workflow.md` §2. Doctrine enforced in-schema: mechanism must be substantive (Law 2), at least one falsifier (Law 4), `min_sample >= 60`. `new_hypothesis_id()` mints sortable ids. `to_runner_dict()` produces the shape the backtest runner consumes. |
| `src/research/hypothesis_forge.py` | `HypothesisForge` — mirrors `src/llm/atlas_brain.py`: forge role + spliced four-lens section form the cached system block; the observation + setup taxonomy go in the user message; output is a validated `Hypothesis`. The forge owns id/provenance. |
| `src/research/store.py` | `HypothesisStore` — saves live hypotheses to `research/hypotheses/<id>.yml` (runner-loadable), retires failed ones to `research/retired/<id>.md` with reason codes and a 90-day re-test policy. |
| `research/hypotheses/.gitkeep`, `research/retired/.gitkeep` | The NEW dirs from the workflow plan, now tracked. |
| `tests/test_hypothesis_forge.py` | 8 tests with a mocked Anthropic client, mirroring `tests/test_atlas_brain.py`. Cover caching, schema doctrine, id minting, and store round-trip/retire. |
| `.gitignore` | Un-ignored `.claude/skills/` so the committed skill is tracked while local `.claude/` state stays ignored. |

---

## 3. How it plugs into the pipeline

```
operator observation ─► HypothesisForge.forge() ─► Hypothesis (validated)
                                                        │ HypothesisStore.save()
                                                        ▼
                                              research/hypotheses/<id>.yml
                                                        │
                                                        ▼
                              BacktestRunner.evaluate_hypothesis()  (src/backtest/runner.py)
                              walk-forward + CPCV + DSR/PBO vs the promotion gate
                                          │                         │
                              PASS ─► memory/setups/        FAIL ─► HypothesisStore.retire()
                                       (candidate filter)           research/retired/<id>.md
```

`src/backtest/runner.py` already declared the hypothesis parser to be "owned by
Track C (research/hypothesis_forge)" — this build supplies that owner. The runner
consumes `Hypothesis.to_runner_dict()`; the promotion gate
(`pbo < 0.20`, `dsr > 0.95`, `wfe_pass_ratio ≥ 0.70`, `min wfe ≥ 0.5`,
OOS trades ≥ 200) is unchanged and authoritative.

---

## 4. How to use it

**Invoke the skill** (in a Claude Code session on this repo): describe a market
observation or research question — "is this London PDH sweep setup real?", "forge a
hypothesis from this Notion card", "why does ATLAS keep losing on X". The skill loads
the doctrine and drives the loop.

**Use the forge directly** in code:

```python
from src.research import HypothesisForge, HypothesisStore

forge = HypothesisForge()                      # uses Anthropic SDK (ANTHROPIC_API_KEY)
h = forge.forge(
    "XAUUSD sweeps PDH in London hour-1 then reverses.",
    slug="ldn-pdh-sweep",
)
path = HypothesisStore().save(h)               # research/hypotheses/<id>.yml
# ... evaluate with BacktestRunner; on FAIL: store.retire(h, verdict["fail_reasons"])
```

**Run the tests:**

```bash
pip install pydantic pytest pyyaml      # plus -r requirements.txt for the full suite
python -m pytest tests/test_hypothesis_forge.py -q
# 8 passed
```

---

## 5. Still pending (intentionally out of scope)

- `scripts/notion_triage.py` — the Notion intake poller that feeds the forge (needs
  the Notion connector + scheduling). The forge accepts the observation string it
  would produce.
- `src/backtest/runner.py` is wired but the simulator/oracle are Phase-0 work; the
  forge does not depend on a finished oracle to produce valid hypotheses.
- The `promo-amplifier` skill (Higgsfield create + X distribute) — referenced as the
  future Phase-4 monetization handoff, gated on a LIVE, audited edge.

---

## 6. Verification

- `python -m pytest tests/test_hypothesis_forge.py -q` → **8 passed**.
- `src.research` imports without the Anthropic SDK installed (lazy client import),
  matching the brain's pattern so tests stay light.
