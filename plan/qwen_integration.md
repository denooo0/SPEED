# Qwen Integration — Three Paths

Qwen3 Max is API-only. The closest open-source Qwen as of May 2026
is **Qwen3.6-27B** (dense) or **Qwen3.6-35B-A3B** (MoE, ~3B active).
All three paths below land in the same `MetaLLMClient` interface so
the meta-loop is model-agnostic.

---

## Path Q-API — Qwen3 Max via Alibaba DashScope or OpenRouter

**Why:** the actual model that finished profitable in Alpha Arena (+22%).
The frontier-grade reasoning we want for high-stakes meta-decisions.

**Cost:** roughly comparable to Claude Opus per-token. OpenRouter
typically slightly cheaper than direct DashScope but adds a hop.

**Wire-up:** OpenAI-compatible API endpoint. Use `httpx` or `openai`
SDK with `base_url=` override.

```python
# Conceptual
from openai import OpenAI
client = OpenAI(
    api_key=os.environ["OPENROUTER_API_KEY"],
    base_url="https://openrouter.ai/api/v1",
)
response = client.chat.completions.create(
    model="qwen/qwen3-max",
    messages=[...],
)
```

**Caveats:**
- Add `OPENROUTER_API_KEY` to env. Never to git.
- Set a hard monthly cost cap.
- The meta-loop runs weekly, not per-trade, so volume is low — should
  fit a $20–50/month budget.

---

## Path Q-Local — Qwen3.6-27B locally via Ollama

**Why:** free, offline, no API limits, no leakage of trade data to a
third party.

**Hardware:** ~24GB VRAM at 4-bit quant (Q4_K_M). Runs on a single
RTX 4090 (24GB) or A6000 (48GB). CPU-only is technically possible but
glacially slow.

**Install** (operator does this on the box that hosts the meta-loop):

```sh
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull Qwen3.6-27B (4-bit quantized)
ollama pull qwen3.6:27b

# Start server (default port 11434)
ollama serve
```

**Wire-up:** Ollama exposes an OpenAI-compatible API.

```python
client = OpenAI(
    api_key="ollama",  # any non-empty string
    base_url="http://localhost:11434/v1",
)
response = client.chat.completions.create(
    model="qwen3.6:27b",
    messages=[...],
)
```

**Caveats:**
- Slower than API path (5–30 seconds per response on a 4090).
- Meta-loop runs offline, so latency doesn't hurt.
- The 27B dense model is the best open weights for quality. The MoE
  35B-A3B is faster per token but slightly weaker reasoning.

---

## Path Q-Self-Host-Pro — Qwen3.6 via vLLM

**Why:** higher throughput than Ollama if the operator runs many meta
calls in batch (e.g., a sweep across 100 proposed configurations).

**Install:**

```sh
pip install vllm
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.6-27B \
  --port 8000 \
  --gpu-memory-utilization 0.9 \
  --max-model-len 8192
```

**Wire-up:** identical to Ollama, point `base_url` at the vLLM server.

**Caveats:**
- More setup pain than Ollama.
- Worth it only if the meta-loop ends up doing batch sweeps.

---

## The MetaLLMClient interface

All three paths flow through one interface so the meta-loop code is
model-agnostic. See `src/meta/llm_client.py`.

Defaults (config-driven):
- Phase M-0 / M-1: Claude Opus 4.7 (already wired, well-tested).
- Phase M-2+: optionally switch to Qwen3 Max via OpenRouter for cost
  efficiency on the higher-volume Tier-B/C work.
- Operator's choice: local Qwen3.6 if they prefer offline.

---

## What about the "actual Qwen3 Max that won Alpha Arena"?

To be precise: Qwen3 Max was Alibaba's flagship 1T-parameter closed
model. The open-source Qwen3.6 is a different model trained by the same
team with different compute and data choices. It is **not** the model
that returned +22% in Alpha Arena.

If we specifically want the Alpha Arena winner, we MUST use the API
(Path Q-API). Path Q-Local gives us a sibling model, not the same one.

This matters for the meta-loop:
- For brain-replacement scenarios: Path Q-API or stay on Claude.
- For batch meta-reasoning at low cost: Path Q-Local is acceptable.
