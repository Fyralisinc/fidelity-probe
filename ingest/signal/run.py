"""Signal slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import SignalConfig
from ..fidelity import FidelityReport
from . import historical, live


def _new_report(cfg: SignalConfig) -> FidelityReport:
    report = FidelityReport("signal", cfg.base_url or "(unset)")
    report.note(
        "Signal (linked-device messaging). Signal has NO official server API and "
        "no maintained pure-Python client; the only sound integration is signal-cli "
        "in JSON-RPC daemon mode (link signal-cli as a secondary device, talk "
        "line-delimited JSON-RPC 2.0 over a socket). But signal-cli is FORWARD-ONLY "
        "— its complete command list has NO backward history-fetch method at all "
        "(`receive`/`subscribeReceive` drain the server's transient queue), so the "
        "backward-paged backfill the ingestion contract assumes is served over a "
        "method-contract shim: HTTP for the reads (get_history backward offset_ts "
        "paging, iter_threads, has_history_since, me) + a WebSocket gateway for the "
        "live receive stream (signal-cli `receive` notifications — no webhook, no "
        "HMAC; the authenticated linked-device session is the trust boundary). The "
        "payloads are the REAL signal-cli envelope shapes (dataMessage / "
        "syncMessage.sentMessage, base64 groupId, sourceUuid actors, timestamp-MS "
        "message ids). A message id IS its timestamp in MILLISECONDS; the dedup "
        "external_id is install-namespaced with the edit slot ALWAYS `none` (Signal "
        "v1 messages are immutable). Signal publishes no per-object JSON-Schema, so "
        "envelopes are validated structurally against the documented semantics.")
    return report


def run_historical() -> FidelityReport:
    cfg = SignalConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = SignalConfig.from_env()
    report = _new_report(cfg)
    report.note("Signal live = the persistent linked-device receive loop (a "
                "WebSocket here): present the session, read the subscribed ack, then "
                "validate each pushed signal-cli `receive` notification. The own "
                "outgoing (syncMessage.sentMessage) messages are skipped server-side. "
                "The negative test is the auth-boundary analog of a webhook-tamper "
                "rejection: a WRONG session must be rejected (signal_api_unauthorized "
                "+ close 4401).")
    print(f"Signal receive gateway: connecting to {cfg.ws_url()} ...")
    if run_seconds is not None:
        print(f"Listening for {run_seconds}s, then writing the fidelity report.")
    else:
        print("Ctrl-C to stop and write the fidelity report.")
    try:
        live.run(cfg, report, run_seconds=run_seconds)
    except KeyboardInterrupt:
        pass
    return report
