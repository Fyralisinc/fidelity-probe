# Ingestion / Fidelity Test Client

A standalone client that connects to **Slack**, **GitHub**, and **Discord** exactly as
production code would, pulls **all historical data** (full pagination + rate-limit
backoff), receives **live events**, validates every payload against each provider's
**official OpenAPI spec**, and emits a **fidelity report** of what was observed.

The point is to be an *independent* tester: it is built only from official SDK + spec
behavior, with **no knowledge of any mock**. The only per-provider knob is the base URL.

## Design rules

- **Official SDKs only:** `slack_sdk`, `PyGithub`, `discord.py`.
- **One knob:** the only per-provider configuration is the base URL (and Discord's gateway
  URL), via env vars. Defaults point at the real production hosts, so no env == real target.
  There are **no mock-specific branches** anywhere.
- **Real auth flows:** Slack OAuth v2, GitHub App-JWT → installation token, Discord bot
  token + gateway IDENTIFY.
- **Strict schema validation** against the providers' official specs. A schema failure is
  only counted as a *divergence* when the spec is authoritative for that payload (its own
  example upholds the schema); otherwise it's recorded as a spec self-inconsistency, not a
  target bug. Undocumented extra fields are recorded for transparency (Slack's spec is
  incomplete and closed, so this is common and expected).
- **Collect-all policy:** every divergence is recorded and ingestion continues; the process
  exits non-zero if any divergence was observed.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python scripts/fetch_specs.py          # cache the 3 official specs into specs/
cp .env.example .env                   # then fill in base URLs / credentials
```

## Run (Slack — the only slice built so far)

```bash
python -m ingest slack historical      # full paginated pull + schema audit
python -m ingest slack live            # Events API webhook listener (signed)
```

Reports are written to `reports/slack.report.{json,md}`. Exit code is non-zero if any
divergence was observed.

### Offline self-check

```bash
python scripts/selfcheck_slack.py      # exercises the full pipeline with no external server
```

## Layout

```
ingest/
  config.py            env-only base URLs/credentials, fail-loud
  schemas.py           official-spec loader + multi-dialect validator (Swagger2/OAS3.0/3.1)
  fidelity.py          report accumulator (-> JSON + Markdown), nonzero exit on divergence
  webhook_server.py    Flask host for signed webhooks
  slack/               auth (OAuth) · historical (cursor pagination) · live (Events API) · run
  github/              (slice 2 — not built yet)
  discord/             (slice 3 — not built yet)
scripts/
  fetch_specs.py       download + pin official OpenAPI specs
  selfcheck_slack.py   dev harness: end-to-end Slack pipeline against a throwaway server
```

## Status

- **Slack:** built (auth, historical, live, schema, report).
- **GitHub / Discord:** planned, paused for review per the agreed Slack-first cadence.
