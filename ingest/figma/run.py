"""Figma slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import FigmaConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: FigmaConfig) -> FidelityReport:
    report = FidelityReport("figma", cfg.base_url or "(unset)")
    report.note("Figma design tool. There is NO GET /v1/files list and NO "
                "/v1/files/{key}/events stream — a real backfill ENUMERATES files "
                "(GET /v1/teams/{id}/projects → GET /v1/projects/{id}/files) then MERGES "
                "GET /v1/files/{key}/versions ({versions:[…], pagination:{prev_page,"
                "next_page}}; CURSOR page_size(def30/max50)+before/after, FULL-URL links) "
                "with GET /v1/files/{key}/comments ({comments:[…]} — NO pagination) into "
                "one event stream (external_id figma:{team}:event:{id}:{version}). The User "
                "object is {id, handle, img_url} with NO email (email is /v1/me only); "
                "timestamps are UTC ISO-8601 with Z; comment.order_id is string|null. Figma "
                "publishes an OpenAPI spec but objects are validated structurally here.")
    return report


def run_historical() -> FidelityReport:
    cfg = FigmaConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = FigmaConfig.from_env()
    report = _new_report(cfg)
    report.note("Figma live = the Webhooks-v2 body-PASSCODE delivery: a plaintext "
                "`passcode` carried as a top-level JSON body field (NO signature header, NO "
                "HMAC — Figma signs nothing). The slice constant-time-compares the passcode "
                "and returns 400 (not 401) on a mismatch (the documented contract). The "
                "metadata-ish events fetch-on-notify: FILE_VERSION_UPDATE → /versions match "
                "version_id; FILE_COMMENT → /comments match comment_id; PING is NOT an "
                "observation. Verified per developers.figma.com/docs/rest-api/webhooks-security.")
    server = WebhookServer("figma-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Figma webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
    if run_seconds is not None:
        print(f"Running for {run_seconds}s, then writing the fidelity report.")
        server.serve_for(wcfg, run_seconds)
        return report
    print("Ctrl-C to stop and write the fidelity report.")
    try:
        server.run(wcfg)
    except KeyboardInterrupt:
        pass
    return report
