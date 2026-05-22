"""Standalone ingestion / fidelity test client.

Connects to Slack, GitHub, and Discord exactly as production code would — the
only per-provider knob is the base URL (env var) — pulls full history and live
events, validates every payload against the provider's official OpenAPI spec,
and emits a fidelity report of what was observed.
"""

__all__ = ["config", "fidelity", "schemas"]
