"""Meta-learning loop.

DESIGN-ONLY MODULE. The interfaces below describe what the loop will be.
Full implementations are blocked on the v9 backtest harness (see
plan/meta_learning_loop.md §V Phase M-0 prerequisites).

The loop is deliberately conservative: every proposed change must pass a
backtest oracle, an adversarial critic, and a human approval gate before
deployment. Tier D (recursive self-modification of the meta-loop's own
prompt) is forbidden by design.
"""
