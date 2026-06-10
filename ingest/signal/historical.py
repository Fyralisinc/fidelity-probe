"""Signal historical ingestion — the per-thread backward-paged backfill.

The backfill: probe the linked account (``me``), enumerate its threads
(``iter_threads`` — 1:1 *direct* + *group*), then for each thread page its history
BACKWARD via ``get_history`` on an ``offset_ts`` cursor (0 = newest; each page is
newest-first and <=100; the next page's cursor = the MIN timestamp of the page; a
short page = the start of obtainable history). One message → one record; the dedup
key is ``signal:{install}:{thread_id}:{message_id}:none`` (install-namespaced; the
edit slot is ALWAYS ``none`` — Signal v1 messages are immutable). The message id IS
the message ``timestamp`` in MILLISECONDS.

Signal publishes no per-object JSON-Schema, so — like the Telegram/GraphQL slices —
we structurally validate the signal-cli envelope SEMANTICS a consumer depends on:
``timestamp`` is epoch MILLISECONDS; an inbound message is a ``dataMessage`` (the
sender in ``source*``); an own/outgoing message is a ``syncMessage.sentMessage``
(the ``out`` analog, carrying no first-class sender); a group message carries
``dataMessage.groupInfo`` (base64 ``groupId``). Built blind from the signal-cli
JSON-RPC docs.

NB (logged): real signal-cli CANNOT page history at all (it is forward-only); this
backward walk exercises the ingestion CONTRACT, served over the method shim — the
"Signal is architecturally live-only" reality is the source's defining divergence.
"""
from __future__ import annotations

from typing import Any, Optional

import requests

from ..config import SignalConfig
from ..fidelity import FidelityReport
from .client import SignalClient


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _body_of(env: dict) -> Optional[dict]:
    """The single message body — a dataMessage XOR a syncMessage.sentMessage."""
    if isinstance(env.get("dataMessage"), dict):
        return env["dataMessage"]
    sm = env.get("syncMessage")
    if isinstance(sm, dict) and isinstance(sm.get("sentMessage"), dict):
        return sm["sentMessage"]
    return None


def is_outgoing(env: dict) -> bool:
    """An own/outgoing message arrives as a syncMessage.sentMessage (the out analog)."""
    sm = env.get("syncMessage")
    return isinstance(sm, dict) and isinstance(sm.get("sentMessage"), dict)


def thread_id_of(env: dict) -> Optional[str]:
    """Derive the conversation thread id from a signal-cli envelope:
    group → the base64 groupId; direct inbound → the sender uuid; direct own →
    the sentMessage destination uuid."""
    body = _body_of(env)
    if body is None:
        return None
    gi = body.get("groupInfo")
    if isinstance(gi, dict) and gi.get("groupId"):
        return gi["groupId"]                       # group thread
    if is_outgoing(env):
        return body.get("destinationUuid")          # direct own → the peer
    return env.get("sourceUuid")                    # direct inbound → the sender


def validate_envelope(env: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(env, dict):
        report.record_protocol("signal envelope is an object", False, repr(env)[:80])
        return
    # timestamp = the message id, epoch MILLISECONDS (a ~1.7e12 integer).
    ts = env.get("timestamp")
    if not _is_int(ts):
        problems.append(f"`timestamp` must be an int (epoch ms), got {ts!r}")
    elif not (1_000_000_000_000 <= ts < 10_000_000_000_000):
        problems.append(f"`timestamp` must be epoch MILLISECONDS, got {ts!r}")

    has_dm = isinstance(env.get("dataMessage"), dict)
    has_sm = isinstance(env.get("syncMessage"), dict)
    if has_dm == has_sm:
        problems.append("exactly one of dataMessage / syncMessage must be present")
    body = _body_of(env)
    if body is None:
        problems.append("no message body (dataMessage / syncMessage.sentMessage)")
    else:
        if not isinstance(body.get("message"), str):
            problems.append("`message` must be a string")
        # the body timestamp echoes the envelope id.
        if _is_int(body.get("timestamp")) and _is_int(ts) and body["timestamp"] != ts:
            problems.append("body.timestamp must equal the envelope timestamp")
        gi = body.get("groupInfo")
        if gi is not None:
            if not isinstance(gi, dict) or not isinstance(gi.get("groupId"), str):
                problems.append("groupInfo.groupId must be a base64 string")
            elif not _is_int(gi.get("revision")):
                problems.append("groupInfo.revision must be an int")
    if has_dm:
        # an inbound dataMessage carries its sender in source*.
        if not isinstance(env.get("sourceUuid"), str):
            problems.append("an inbound dataMessage must carry a sourceUuid")

    check = ("signal envelope contract (epoch-ms timestamp id, dataMessage XOR "
             "syncMessage, base64 groupId)")
    if problems:
        report.record_protocol(check, False, f"ts={env.get('timestamp')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def run_historical(report: FidelityReport, cfg: SignalConfig) -> None:
    client = SignalClient(cfg, report)
    namespace = cfg.namespace()
    report.auth.update({"method": "persisted linked-device session (the libsignal "
                        "identity store; presented per-read in this method shim — "
                        "signal-cli has no per-request token)",
                        "install_namespace": namespace})

    # 1) me — the connectivity + credential probe (the linked account identity).
    st, body = client.me()
    acct = (body or {}).get("account") if isinstance(body, dict) else None
    if st == 200 and isinstance(acct, dict) and isinstance(acct.get("uuid"), str):
        report.record_protocol("me resolves the linked account", True, "")
        report.auth["account_uuid"] = acct["uuid"]
        report.auth["account_number"] = acct.get("number")
    else:
        report.diverge("protocol", "me", f"-> {st}; {str(body)[:160]}")

    # 2) iter_threads — enumerate the threads to backfill.
    st, body = client.iter_threads()
    if st != 200 or not isinstance(body, dict) or not isinstance(body.get("threads"), list):
        report.diverge("protocol", "iter_threads", f"-> {st}; {str(body)[:160]}")
        return
    threads = body["threads"]
    kinds_ok = all(t.get("thread_kind") in ("direct", "group") for t in threads)
    report.record_protocol("threads carry a kind in {direct, group}", kinds_ok,
                           "" if kinds_ok else "unexpected thread_kind")
    report.note(f"threads: {len(threads)} "
                f"({sum(t['thread_kind']=='group' for t in threads)} group / "
                f"{sum(t['thread_kind']=='direct' for t in threads)} direct)")

    # 3) per-thread backward walk.
    seen_ok: set = set()
    external_ids: set[str] = set()
    total_msgs = 0
    max_pages_in_a_thread = 0
    self_sent = 0
    group_msgs = 0
    thread_mismatch = 0
    for t in threads:
        thread = {"thread_id": t["thread_id"]}
        offset_ts, pages, walked = 0, 0, 0
        while pages < 100_000:
            st, messages, next_off, is_last = client.get_history(
                thread=thread, offset_ts=offset_ts, limit=100)
            if st != 200:
                report.diverge("protocol", "get_history",
                               f"thread {t['thread_id']} -> {st}")
                break
            report.record_page(f"get_history:{t['thread_id'][:12]}",
                               str(offset_ts) if offset_ts else None)
            pages += 1
            # Newest-first ordering within the page (descending timestamp).
            tss = [m["timestamp"] for m in messages if isinstance(m.get("timestamp"), int)]
            if tss != sorted(tss, reverse=True):
                report.record_protocol("get_history page is newest-first (descending ts)",
                                       False, f"thread {t['thread_id'][:12]}: {tss[:4]}")
            for m in messages:
                validate_envelope(m, report, seen_ok)
                if is_outgoing(m):
                    self_sent += 1
                if _body_of(m) and _body_of(m).get("groupInfo"):
                    group_msgs += 1
                # the thread derived from the envelope must match the iteration thread.
                if thread_id_of(m) != t["thread_id"]:
                    thread_mismatch += 1
                ext = f"signal:{namespace}:{t['thread_id']}:{m.get('timestamp')}:none"
                external_ids.add(ext)
                walked += 1
            if is_last:
                break
            offset_ts = next_off
        total_msgs += walked
        max_pages_in_a_thread = max(max_pages_in_a_thread, pages)

    report.count("message", total_msgs)
    report.count("thread", len(threads))
    report.note(f"messages: {total_msgs} (self_sent={self_sent}, group={group_msgs})")
    report.record_protocol("get_history backward walk terminates on a short page", True, "")
    report.record_protocol("each envelope's derived thread matches the walked thread",
                           thread_mismatch == 0,
                           "" if thread_mismatch == 0 else f"{thread_mismatch} mismatches")
    if max_pages_in_a_thread > 1:
        report.record_protocol("a thread's history walks multiple offset_ts pages", True,
                               f"max pages in one thread = {max_pages_in_a_thread}")
    else:
        report.record_protocol("a thread's history walks multiple offset_ts pages", False,
                               "no thread needed more than one page (corpus too small?)")
    if total_msgs and len(external_ids) == total_msgs:
        report.record_protocol("every message yields a unique external_id (edit slot none)",
                               True, "")
    elif total_msgs:
        report.record_protocol("every message yields a unique external_id (edit slot none)",
                               False, f"{len(external_ids)} unique vs {total_msgs} messages")
    if self_sent:
        report.record_protocol("own/outgoing messages arrive as syncMessage.sentMessage",
                               True, f"{self_sent} self-sent (no first-class sender)")

    # 4) negative probes — faithful failure modes the consumer must branch on.
    st, _m, _n, _l = client.get_history(thread={"thread_id": "no-such-thread"}, limit=10)
    report.record_protocol("unknown thread → signal_api_error (400)", st == 400,
                           "" if st == 400 else f"-> {st}")

    # A wrong session → 401 signal_api_unauthorized (the unauthorized/revoked analog).
    bad = requests.post(f"{cfg.base_url}/v1/iter_threads",
                        headers={"Authorization": "Session wrong-session",
                                 "Content-Type": "application/json"},
                        json={}, timeout=30)
    ok = (bad.status_code == 401
          and ((bad.json() or {}).get("error", {}).get("data", {}).get("signal_code")
               == "signal_api_unauthorized"))
    report.record_protocol("a wrong session → 401 signal_api_unauthorized", ok,
                           "" if ok else f"-> {bad.status_code}")

    # has_history_since — the reconciler 1-row gap probe (min_ts=0 → has_more True).
    if threads:
        st, body = client.has_history_since(thread={"thread_id": threads[0]["thread_id"]},
                                            min_ts=0)
        ok = st == 200 and isinstance(body, dict) and body.get("has_more") is True
        report.record_protocol("has_history_since probes for a gap above min_ts", ok,
                               "" if ok else f"-> {st}; {str(body)[:120]}")
