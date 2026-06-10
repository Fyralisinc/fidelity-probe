"""Ashby slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import AshbyConfig, WebhookConfig
from ..fidelity import FidelityReport
from ..webhook_server import WebhookServer
from . import historical, live


def _new_report(cfg: AshbyConfig) -> FidelityReport:
    report = FidelityReport("ashby", cfg.base_url or "(unset)")
    report.note("Ashby recruiting / ATS. An RPC-style API: every read is an HTTP POST "
                "to /<category>.<verb>. Entities (candidate/application/job/interview/"
                "offer) are pulled via /<category>.list (cursor-paged; {success, "
                "results:[…], moreDataAvailable, nextCursor?, syncToken?}) with an "
                "incremental syncToken minted on the terminal page; /<category>.info "
                "returns a single results OBJECT. Auth is the API key as the HTTP Basic "
                "username with an empty password. Ashby publishes an OpenAPI but no "
                "per-object JSON-Schema, so objects are validated structurally against "
                "the documented shape + enums.")
    return report


def run_historical() -> FidelityReport:
    cfg = AshbyConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = AshbyConfig.from_env()
    report = _new_report(cfg)
    report.note("Ashby live = the HMAC-signed webhook: a {action, data:{<entity>:{…}}} "
                "delivery signed Ashby-Signature = sha256=<lowercase-hex HMAC-SHA256 over "
                "the RAW body> (the sha256= prefix IS present; no timestamp/replay window). "
                "Verified per developer.ashbyhq.com/docs/authenticating-webhooks.")
    server = WebhookServer("ashby-webhooks")
    live.register(server, cfg, report)
    wcfg = WebhookConfig.from_env()
    print(f"Ashby webhook listener on http://{wcfg.host}:{wcfg.port}{live.ENDPOINT}")
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
