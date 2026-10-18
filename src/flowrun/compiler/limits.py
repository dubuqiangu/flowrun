"""Compile-time numeric upper bounds for flow.mmd values.

A Mermaid source is untrusted input: a `timeout:`/`budget_tokens`/`retry:`
value is copied straight into the IR and then into the run loop, so an
unbounded value is a cost/availability hazard. Left uncapped:

- ``timeout: <400 nines>`` overflows ``float()`` to ``inf``, and a hop that can
  never time out pins a worker forever;
- ``budget_tokens`` has no ceiling, so a typo silently authorises an arbitrary
  token spend;
- ``retry: <huge>`` turns a failing node into an unbounded attempt loop.

These caps are deliberately generous (day-scale timeout, 10^9 tokens,
two-digit retries): they reject only values that are meaningless rather than
merely large. A value exactly equal to a cap is accepted; only values strictly
above it are rejected.

Kept in this standalone module so the bounds are stated once and both the
parser and the validator read the same constants.
"""

from __future__ import annotations

# 30 days: long enough for slow batch/eval hops, short enough to still be a
# bound. The sample flow and every existing fixture use minutes at most.
MAX_TIMEOUT_SECONDS = 30 * 24 * 60 * 60  # 2_592_000 seconds

# 10^9 tokens, an order of magnitude above the 10^6 default budget: protects
# against a digit-count typo without constraining realistic long runs.
MAX_BUDGET_TOKENS = 1_000_000_000

# Two-digit attempt ceiling; anything larger is a runaway retry loop.
MAX_RETRY_ATTEMPTS = 99
