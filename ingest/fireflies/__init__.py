"""Fireflies.ai ingestion slice — the REAL GraphQL API.

Fireflies' real surface is a single ``POST https://api.fireflies.ai/graphql``
exposing ``transcripts``/``transcript``/``user`` queries (NOT a REST surface).
Built blind from the official docs (docs.fireflies.ai) — see run.py for the
contract summary + the logged Fyralis-vs-real divergences.
"""
