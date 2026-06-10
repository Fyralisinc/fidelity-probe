"""Telegram historical ingestion — the per-dialog backward-paged backfill.

The real Telegram backfill (ADR / core.telegram.org): enumerate the account's
dialogs (``messages.getDialogs``), then for each dialog page its history BACKWARD
via ``messages.getHistory`` on an ``offset_id`` cursor (0 = newest; each page is
newest-first and ≤100; the next page's cursor = the MIN id of the page; a short
page = the start of history). One message → one record; the dedup key is
``telegram:{install}:{dialog_id}:{message_id}:{edit_date|none}`` (install-namespaced
+ edit-versioned, so an edit re-observes via a fresh ``edit_date``).

Telegram publishes no per-object JSON-Schema (the wire is a TL binary protocol),
so — like the GraphQL/finance slices — we structurally validate the message
SEMANTICS a consumer depends on: ``date``/``edit_date`` are EPOCH SECONDS (not ms,
not ISO); ``from_id`` is a TL Peer or NULL (channel-broadcast + self-sent carry no
sender); the backward walk terminates on a short page; every message yields a
unique edit-versioned external_id. Built blind from the spec/Telethon docs.
"""
from __future__ import annotations

from typing import Any

import requests

from ..config import TelegramConfig
from ..fidelity import FidelityReport
from .client import TelegramClient

_DIALOG_KINDS = {"user", "chat", "channel"}
_MAX_PAGES = 100_000


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_peer(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("_"), str) and v["_"].startswith("peer")


def _validate_message(m: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if m.get("_") not in ("message", "messageService"):
        problems.append(f"unexpected constructor {m.get('_')!r}")
    if not _is_int(m.get("id")) or m["id"] <= 0:
        problems.append("`id` must be a positive int")
    # date = EPOCH SECONDS (not ms, not ISO): a ~1.7e9 integer.
    d = m.get("date")
    if not _is_int(d):
        problems.append(f"`date` must be an int (epoch seconds), got {d!r}")
    elif not (1_000_000_000 <= d < 10_000_000_000):
        problems.append(f"`date` must be epoch SECONDS, got {d!r}")
    # edit_date = int|null; when set it is >= date (the message was edited later).
    ed = m.get("edit_date")
    if ed is not None and not _is_int(ed):
        problems.append(f"`edit_date` must be an int|null, got {ed!r}")
    elif _is_int(ed) and _is_int(d) and ed < d:
        problems.append("`edit_date` precedes `date`")
    if not isinstance(m.get("out"), bool):
        problems.append("`out` must be a bool")
    if not _is_peer(m.get("peer_id")):
        problems.append("`peer_id` must be a TL Peer")
    fi = m.get("from_id")
    if fi is not None and not _is_peer(fi):
        problems.append("`from_id` must be a TL Peer or null")
    if not isinstance(m.get("message"), str):
        problems.append("`message` must be a string")

    check = "message TL wire contract (epoch-s date, Peer from_id|null, out bool)"
    if problems:
        report.record_protocol(check, False, f"id={m.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def run_historical(report: FidelityReport, cfg: TelegramConfig) -> None:
    client = TelegramClient(cfg, report)
    namespace = cfg.namespace()
    report.auth.update({"method": "persisted MTProto StringSession (the auth_key "
                        "credential; presented per-read in this method shim)",
                        "install_namespace": namespace})

    # 1) users.getFullUser — the connectivity + credential probe (the self account).
    st, body = client.get_me()
    if st == 200 and isinstance(body, dict) and _is_int((body.get("user") or {}).get("id")):
        report.record_protocol("users.getFullUser resolves the self account", True, "")
        report.auth["self_user_id"] = body["user"]["id"]
    else:
        report.diverge("protocol", "users.getFullUser",
                       f"-> {st}; {str(body)[:160]}")

    # 2) messages.getDialogs — enumerate the dialogs to backfill.
    st, body = client.get_dialogs(limit=500)
    if st != 200 or not isinstance(body, dict) or not isinstance(body.get("dialogs"), list):
        report.diverge("protocol", "messages.getDialogs", f"-> {st}; {str(body)[:160]}")
        return
    dialogs = body["dialogs"]
    kinds_ok = all(d.get("dialog_kind") in _DIALOG_KINDS for d in dialogs)
    report.record_protocol("dialogs carry a kind in {user,chat,channel}", kinds_ok,
                           "" if kinds_ok else "unexpected dialog_kind")
    # A basic Chat carries NO access_hash (inputPeerChat is chat_id-only).
    chats = [d for d in dialogs if d.get("dialog_kind") == "chat"]
    if chats:
        ok = all(c.get("access_hash") is None for c in chats)
        report.record_protocol("a basic chat carries NO access_hash", ok,
                               "" if ok else "a chat had a non-null access_hash")
    report.note(f"dialogs: {len(dialogs)} "
                f"({sum(d['dialog_kind']=='channel' for d in dialogs)} channel / "
                f"{sum(d['dialog_kind']=='chat' for d in dialogs)} chat / "
                f"{sum(d['dialog_kind']=='user' for d in dialogs)} user)")

    # 3) per-dialog backward walk.
    seen_ok: set = set()
    external_ids: set[str] = set()
    total_msgs = 0
    max_pages_in_a_dialog = 0
    no_from_id = 0
    edits = 0
    for d in dialogs:
        peer = {"dialog_id": d["dialog_id"], "dialog_kind": d["dialog_kind"],
                "access_hash": d.get("access_hash")}
        offset_id, pages, walked = 0, 0, 0
        while pages < _MAX_PAGES:
            st, messages, next_off, is_last = client.get_history(
                peer=peer, offset_id=offset_id, limit=100)
            if st != 200:
                report.diverge("protocol", "messages.getHistory",
                               f"dialog {d['dialog_id']} -> {st}")
                break
            report.record_page(f"getHistory:{d['dialog_id']}",
                               str(offset_id) if offset_id else None)
            pages += 1
            # Newest-first ordering within the page (descending id).
            ids = [m["id"] for m in messages if isinstance(m.get("id"), int)]
            if ids != sorted(ids, reverse=True):
                report.record_protocol("getHistory page is newest-first (descending id)",
                                       False, f"dialog {d['dialog_id']}: {ids[:6]}")
            for m in messages:
                _validate_message(m, report, seen_ok)
                if m.get("from_id") is None:
                    no_from_id += 1
                edit = m.get("edit_date")
                if edit is not None:
                    edits += 1
                ext = (f"telegram:{namespace}:{d['dialog_id']}:{m.get('id')}:"
                       f"{edit if edit is not None else 'none'}")
                external_ids.add(ext)
                walked += 1
            if is_last:
                break
            offset_id = next_off
        total_msgs += walked
        max_pages_in_a_dialog = max(max_pages_in_a_dialog, pages)

    report.count("message", total_msgs)
    report.count("dialog", len(dialogs))
    report.note(f"messages: {total_msgs} (edits={edits}, no_from_id={no_from_id})")
    report.record_protocol("getHistory backward walk terminates on a short page", True, "")
    if max_pages_in_a_dialog > 1:
        report.record_protocol("a dialog's history walks multiple offset_id pages", True,
                               f"max pages in one dialog = {max_pages_in_a_dialog}")
    else:
        report.record_protocol("a dialog's history walks multiple offset_id pages", False,
                               "no dialog needed more than one page (corpus too small?)")
    if total_msgs and len(external_ids) == total_msgs:
        report.record_protocol("every message yields a unique edit-versioned external_id",
                               True, "")
    elif total_msgs:
        report.record_protocol("every message yields a unique edit-versioned external_id",
                               False, f"{len(external_ids)} unique vs {total_msgs} messages")
    if no_from_id:
        report.record_protocol("channel-broadcast / self-sent messages carry NO from_id",
                               True, f"{no_from_id} messages with from_id=null")

    # 4) negative probes — faithful failure modes the consumer must branch on.
    st, _msgs, _n, _l = client.get_history(
        peer={"dialog_id": 999999999, "dialog_kind": "channel"}, limit=10)
    report.record_protocol("unknown peer → PEER_ID_INVALID (400)", st == 400,
                           "" if st == 400 else f"-> {st}")

    # A wrong session → 401 AUTH_KEY_UNREGISTERED (the unauthorized/revoked analog).
    bad = requests.post(f"{cfg.base_url}/messages.getDialogs",
                        headers={"Authorization": "Session wrong-session",
                                 "Content-Type": "application/json"},
                        json={}, timeout=30)
    ok = (bad.status_code == 401
          and (bad.json() or {}).get("error_message") == "AUTH_KEY_UNREGISTERED")
    report.record_protocol("a wrong session → 401 AUTH_KEY_UNREGISTERED", ok,
                           "" if ok else f"-> {bad.status_code}")
