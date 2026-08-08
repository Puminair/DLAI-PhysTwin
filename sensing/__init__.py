"""Layer 2 — sensing: what can be observed, and how badly.

Imports world/ only to *observe* it. This layer never emits facts — it
emits observations about facts, with noise, latency and gaps. Every
record carries a `gap` field when something was withheld and a
`confidence` of "observed" or "inferred".
"""
