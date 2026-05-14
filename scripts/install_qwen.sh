#!/usr/bin/env bash
# Install Qwen3.6-27B locally via Ollama for the meta-loop's offline path.
#
# Qwen3 Max (the +22% Alpha Arena winner) is NOT open-source — API only.
# Qwen3.6-27B is the closest open-weight model under Apache 2.0.
#
# Requires: ~24GB VRAM (RTX 4090 / A6000 / dual-3090 / H100).
# CPU-only is technically possible but glacially slow — not recommended.

set -euo pipefail

echo "=== ATLAS meta-loop: Qwen3.6 local install ==="
echo

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama not found. Installing..."
  curl -fsSL https://ollama.com/install.sh | sh
else
  echo "Ollama already installed: $(ollama --version)"
fi

echo
echo "Pulling Qwen3.6-27B (dense, ~17GB at Q4_K_M quant)..."
ollama pull qwen3.6:27b

echo
echo "Verifying model is loaded..."
ollama list | grep -i qwen3.6 || {
  echo "ERROR: qwen3.6 not in ollama list. Check pull output."
  exit 1
}

echo
echo "Starting Ollama server in background (port 11434)..."
if pgrep -f "ollama serve" >/dev/null 2>&1; then
  echo "Ollama server already running."
else
  nohup ollama serve >/dev/null 2>&1 &
  sleep 2
fi

echo
echo "Smoke test (one short prompt)..."
curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6:27b",
    "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
    "max_tokens": 8
  }' | head -200

echo
echo
echo "=== Done. To use in the meta-loop: ==="
echo "  Set in config.yaml -> META:"
echo "    provider: ollama"
echo "    model: qwen3.6:27b"
echo "    base_url: http://localhost:11434/v1"
echo
echo "For Qwen3 Max via API instead, see plan/qwen_integration.md path Q-API."
