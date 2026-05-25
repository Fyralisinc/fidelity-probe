# Ingestion / Fidelity Test Client

A standalone client that connects to **Slack**, **GitHub**, and **Discord** exactly as
production code would, pulls **all historical data** (full pagination + rate-limit
backoff), receives **live events**, validates every payload against each provider's
**official OpenAPI spec**, and emits a **fidelity report** of what was observed.

The point is to be an *independent* tester: it is built only from official SDK + spec
behavior, with **no knowledge of any mock**. The only per-provider knob is the base URL.

## Design rules

- **Official SDKs / documented wire:** `slack_sdk`, `PyGithub`, `discord.py` where an
  official Python SDK exists; Google (Gmail/Calendar/Directory) and Notion have no first-
  party Python SDK, so they're built against the official documented REST contracts and
  auth flows (matching what google-auth/the SDKs do on the wire).
- **One knob:** the only per-provider configuration is the base URL(s), via env vars.
  Defaults point at the real production hosts, so no env == real target. There are **no
  mock-specific branches** anywhere.
- **Real auth flows:** Slack OAuth v2, GitHub App-JWT → installation token, Discord bot
  token + gateway IDENTIFY, Google service-account JWT-bearer + domain-wide delegation,
  Notion internal-integration bearer.
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

## Run

```bash
python -m ingest slack historical      # full paginated pull + schema audit
python -m ingest slack live            # Events API webhook listener (signed)

python -m ingest github historical     # App-JWT auth + Link-paginated REST audit
python -m ingest github live           # webhook listener (X-Hub-Signature-256)

python -m ingest discord historical    # guilds→channels→messages, snowflake pagination
python -m ingest discord live          # gateway listener (IDENTIFY→READY→events)

python -m ingest gmail historical      # SA+DWD → directory enumerate → messages backfill
python -m ingest calendar historical   # SA+DWD → events backfill + syncToken incremental
python -m ingest notion historical     # Bearer + Notion-Version → search → full objects
```

Gmail and Calendar authenticate with a Google **service account using domain-wide
delegation**: a signed JWT assertion is exchanged at the token endpoint for a per-user
bearer (`sub` = the impersonated user), mailboxes/users are enumerated through the Admin
Directory API, and reads use read-only scopes. Calendar additionally exercises the
`syncToken` incremental path and the expired-token (`410 fullSyncRequired`) path. Notion
uses a single internal integration (`Authorization: Bearer …`, pinned
`Notion-Version: 2022-06-28`). Gmail/Calendar accept `--max-users` to bound a run; live
push/webhook delivery is not wired yet. Google responses are validated against the
official **discovery documents**; Notion against contracts hand-authored from its API
reference (no official OpenAPI exists).

The Discord historical run logs in with the Bot token, then pages every guild's
channels and message history (snowflake `before`/`after` cursors), validating each
payload against the official OpenAPI 3.1 spec. discord.py has no base-URL parameter,
so the slice redirects both transports — REST (`Route.BASE`) and the gateway
(`DiscordWebSocket.DEFAULT_GATEWAY`) — at the target; nothing else about the library
changes. The live run connects the real gateway and schema-validates `MESSAGE_CREATE`
payloads against `MessageResponse`.

The GitHub historical run performs the real two-legged App auth (RS256 App JWT →
`POST /app/installations/{id}/access_tokens` → `ghs_` token), then reads every
installation repo's issues/PRs/commits/branches/labels with `Link` pagination,
verifies `ETag`/`304 Not Modified` conditional requests, and audits the standard
GitHub response headers — all validated against the official OpenAPI 3.0 spec.

Reports are written to `reports/<provider>.report.{json,md}`. Exit code is non-zero
if any divergence was observed.

### Offline self-checks

```bash
python scripts/selfcheck_slack.py      # full Slack pipeline against a throwaway server
python scripts/selfcheck_github.py     # full GitHub pipeline (incl. JWT auth + ETag/304)
python scripts/selfcheck_discord.py    # full Discord pipeline (REST + real gateway handshake)
```

## Layout

```
ingest/
  config.py            env-only base URLs/credentials, fail-loud
  schemas.py           official-spec loader + multi-dialect validator (Swagger2/OAS3.0/3.1)
  fidelity.py          report accumulator (-> JSON + Markdown), nonzero exit on divergence
  webhook_server.py    Flask host for signed webhooks
  slack/               auth (OAuth) · historical (cursor pagination) · live (Events API) · run
  github/              auth (App JWT → installation token) · historical (Link pagination,
                       ETag/304, header audit) · live (signed webhooks) · run
  discord/             auth (Bot token + REST/gateway redirect) · historical (snowflake
                       pagination) · live (gateway IDENTIFY→events) · run
  google/              auth (SA JWT-bearer + DWD) · directory (enumerate users) ·
                       gmail · calendar (events + syncToken) · transport (429) · run
  notion/              client (Bearer + Notion-Version + 429) · historical · run
scripts/
  fetch_specs.py       download + pin official OpenAPI specs
  selfcheck_slack.py   dev harness: end-to-end Slack pipeline against a throwaway server
  selfcheck_github.py  dev harness: end-to-end GitHub pipeline (incl. App-JWT auth)
  selfcheck_discord.py dev harness: end-to-end Discord pipeline (REST + fake gateway)
```

## Status

- **Slack:** built (auth, historical, live, schema, report).
- **GitHub:** built (App-JWT auth, historical with Link pagination + ETag/304 + header
  audit, live webhooks, schema, report).
- **Discord:** built (Bot-token auth with REST + gateway redirection, snowflake-paginated
  historical, live gateway events, schema, report).
- **Gmail / Calendar:** built (service-account + domain-wide-delegation auth, Admin
  Directory enumeration, message/event backfill, Calendar syncToken incremental +
  expired-token paths, discovery-schema validation, report). Live push deferred.
- **Notion:** built (internal-integration auth, search/enumerate → paginate → full-object
  fetch, 429/Retry-After handling, schema, report). Live webhook deferred.
