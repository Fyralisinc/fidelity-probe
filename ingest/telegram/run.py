"""Telegram slice orchestration: build config, run historical/live, emit report."""
from __future__ import annotations

from ..config import TelegramConfig
from ..fidelity import FidelityReport
from . import historical, live


def _new_report(cfg: TelegramConfig) -> FidelityReport:
    report = FidelityReport("telegram", cfg.base_url or "(unset)")
    report.note(
        "Telegram (MTProto user-account API, consumed via Telethon). The real "
        "transport is the MTProto encrypted BINARY protocol — there is no HTTP "
        "REST API and the Bot API can't read history, so it isn't used. "
        "Reproducing the binary wire is infeasible (full TL/DH/AES-IGE server) and "
        "is not how the source is tested even in-house, so the target reproduces "
        "the MTProto METHOD contract over a transport substitution: HTTP for the "
        "request/response reads (messages.getHistory backward offset_id paging, "
        "messages.getDialogs, users.getFullUser) + a WebSocket updates gateway for "
        "the live push (updateNewMessage/updateEditMessage — no webhook, no HMAC; "
        "the authenticated connection is the trust boundary). The credential is a "
        "persisted Telethon StringSession. Message date/edit_date are EPOCH SECONDS; "
        "from_id is a TL Peer or NULL (channel-broadcast + self-sent carry none); "
        "the dedup external_id is install-namespaced + edit-versioned. Telegram "
        "publishes no per-object JSON-Schema (TL binary), so messages are validated "
        "structurally against the documented semantics.")
    return report


def run_historical() -> FidelityReport:
    cfg = TelegramConfig.from_env()
    report = _new_report(cfg)
    historical.run_historical(report, cfg)
    return report


def run_live(run_seconds: float | None = None) -> FidelityReport:
    cfg = TelegramConfig.from_env()
    report = _new_report(cfg)
    report.note("Telegram live = the persistent MTProto updates connection (a "
                "WebSocket here): present the StringSession, read updates.state, "
                "then validate each pushed updateNewMessage/updateEditMessage. The "
                "negative test is the auth-boundary analog of a webhook-tamper "
                "rejection: a WRONG session must be rejected (AUTH_KEY_UNREGISTERED "
                "+ close 4401).")
    print(f"Telegram updates gateway: connecting to {cfg.ws_url()} ...")
    if run_seconds is not None:
        print(f"Listening for {run_seconds}s, then writing the fidelity report.")
    else:
        print("Ctrl-C to stop and write the fidelity report.")
    try:
        live.run(cfg, report, run_seconds=run_seconds)
    except KeyboardInterrupt:
        pass
    return report
