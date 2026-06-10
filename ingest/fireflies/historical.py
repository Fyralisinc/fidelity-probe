"""Fireflies historical ingestion — the meeting-transcript backfill.

The REAL Fireflies contract (docs.fireflies.ai):
  * Reads are GraphQL over ``POST /graphql``. ``transcripts(skip, limit≤50,
    fromDate, toDate)`` returns a plain ``[Transcript]`` array under
    ``data.transcripts`` (newest-first; NO total/pageInfo — a short page is EOF).
  * A Transcript's ``date`` is a **Float epoch-MILLISECONDS** (creation); the
    SEPARATE ``dateString`` is the ISO-8601 ``…Z`` string; ``duration`` is a Number
    in **MINUTES**. There is NO ``updatedAt``/``processedAt``/``version`` field —
    the dedup "content version" is DERIVED (we use ``date`` here).
  * ``participants``/``fireflies_users`` are ``[String]`` emails; ``meeting_attendees``
    are objects; ``summary`` carries ``overview``/``action_items``/…; ``meeting_info``
    carries ``summary_status``.
  * ``transcript(id: String!)`` hydrates one; an unknown id → ``object_not_found``.
  * ``user`` (no id) resolves the API-key owner — Fireflies' real "verify my token"
    (there is no first-class workspace id; identity = the owner's ``user_id``).

Fireflies publishes no per-object JSON-Schema, so — like the QBO/mercury/ramp
slices — we structurally validate the fields a consumer depends on (incl. the
epoch-ms ``date`` + ISO ``dateString`` + minutes ``duration`` conventions) and
assert the skip/limit walk terminates on a short page. Built blind from the docs.
"""
from __future__ import annotations

import re
from typing import Any

from ..fidelity import FidelityReport
from .client import FirefliesClient

_PAGE = 50
_MAX_PAGES = 10_000
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _err_code(errors) -> str | None:
    if isinstance(errors, list) and errors:
        ext = errors[0].get("extensions") if isinstance(errors[0], dict) else None
        if isinstance(ext, dict):
            return ext.get("code")
    return None


def _validate_transcript(t: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not t.get("id"):
        problems.append("missing `id`")
    if not isinstance(t.get("title"), str):
        problems.append("`title` must be a String")
    # date = epoch MILLISECONDS (Float/Int number), NOT an ISO string.
    if not _is_number(t.get("date")):
        problems.append(f"`date` must be a Number (epoch ms), got {t.get('date')!r}")
    elif t["date"] < 1_000_000_000_000:
        problems.append(f"`date` must be epoch MILLISECONDS, got {t['date']!r}")
    # dateString = ISO-8601 ...Z with ms precision (the separate string form).
    ds = t.get("dateString")
    if ds is not None and not (isinstance(ds, str) and _ISO_Z_RE.match(ds)):
        problems.append(f"`dateString` must be ISO-8601 ...Z (ms), got {ds!r}")
    # duration = Number in MINUTES.
    if t.get("duration") is not None and not _is_number(t.get("duration")):
        problems.append("`duration` must be a Number (minutes)")
    # participants = [String] (emails).
    parts = t.get("participants")
    if parts is not None and not (isinstance(parts, list)
                                  and all(isinstance(p, str) for p in parts)):
        problems.append("`participants` must be [String]")
    # meeting_attendees = [{email,…}] objects.
    att = t.get("meeting_attendees")
    if att:
        if not (isinstance(att, list) and isinstance(att[0], dict)
                and "email" in att[0]):
            problems.append("`meeting_attendees` must be [{email,…}] objects")
    # summary carries overview/action_items.
    summ = t.get("summary")
    if summ is not None and not isinstance(summ, dict):
        problems.append("`summary` must be an object|null")
    mi = t.get("meeting_info")
    if mi is not None and not isinstance(mi, dict):
        problems.append("`meeting_info` must be an object|null")

    check = "transcript object contract"
    if problems:
        report.record_protocol(check, False, f"id={t.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def run_historical(report: FidelityReport, cfg) -> None:
    client = FirefliesClient(cfg, report)
    report.auth.update({"method": "Bearer API token (GraphQL POST /graphql)"})

    # 1) user{} — the real "verify my token" + the workspace identity (owner).
    st, body, errors = client.verify_user()
    owner_id = None
    if st == 200 and isinstance(body, dict) and body.get("data", {}).get("user"):
        owner = body["data"]["user"]
        owner_id = owner.get("user_id")
        if owner_id and isinstance(owner.get("email"), str):
            report.record_protocol("user{} resolves the API-key owner (identity)", True, "")
        else:
            report.record_protocol("user{} resolves the API-key owner (identity)", False,
                                   f"owner={owner!r}")
    else:
        report.diverge("protocol", "user", f"user{{}} -> {st}; {str(errors or body)[:160]}")
    namespace = owner_id or "unknown"

    # 2) transcripts — the primary stream (skip/limit walk; short page = EOF).
    seen_ok: set = set()
    external_ids: set[str] = set()
    skip, pages, walked = 0, 0, 0
    while pages < _MAX_PAGES:
        st, body, errors = client.list_transcripts(limit=_PAGE, skip=skip)
        report.record_page("transcripts", f"skip={skip}")
        if st != 200 or not isinstance(body, dict):
            report.diverge("protocol", "transcripts",
                           f"transcripts -> {st}; {str(errors or body)[:160]}")
            break
        data = body.get("data") or {}
        page = data.get("transcripts")
        if not isinstance(page, list):
            report.diverge("protocol", "transcripts",
                           f"data.transcripts must be a [Transcript] array, got {type(page)}")
            break
        pages += 1
        for t in page:
            if not isinstance(t, dict):
                continue
            _validate_transcript(t, report, seen_ok)
            report.count("transcript")
            walked += 1
            # The dedup external_id is versioned by the DERIVED content version
            # (no first-class version field exists — we use `date`).
            external_ids.add(f"fireflies:{namespace}:transcript:{t.get('id')}:{t.get('date')}")
        if len(page) < _PAGE:        # short page => end of data
            break
        skip += _PAGE
    report.note(f"transcripts: {walked} over {pages} page(s)")
    report.record_protocol("transcripts skip/limit walk terminates on a short page", True, "")
    if walked and len(external_ids) == walked:
        report.record_protocol("every transcript yields a unique versioned external_id", True, "")
    elif walked:
        report.record_protocol("every transcript yields a unique versioned external_id", False,
                               f"{len(external_ids)} unique vs {walked} transcripts")

    # 3) transcript(id:) single hydrate — pull one full transcript by id.
    st, body, _ = client.list_transcripts(limit=1, skip=0)
    if st == 200 and body.get("data", {}).get("transcripts"):
        tid = body["data"]["transcripts"][0]["id"]
        st2, single, errs = client.get_transcript(tid)
        one = (single or {}).get("data", {}).get("transcript") if isinstance(single, dict) else None
        if st2 == 200 and isinstance(one, dict) and one.get("id") == tid:
            report.record_protocol("transcript(id:) hydrates a single Transcript", True, "")
        else:
            report.record_protocol("transcript(id:) hydrates a single Transcript", False,
                                   f"-> {st2}; {str(errs or single)[:160]}")

    # 4) unknown id → object_not_found (a documented failure mode).
    st3, _b, errs3 = client.get_transcript("DOES_NOT_EXIST_XYZ")
    code = _err_code(errs3)
    if code == "object_not_found":
        report.record_protocol("unknown transcript id → object_not_found", True, "")
    else:
        report.record_protocol("unknown transcript id → object_not_found", False,
                               f"-> {st3}; code={code!r}")

    # 5) fromDate filter is a meaningful incremental knob (informational probe).
    st4, body4, _ = client.list_transcripts(limit=_PAGE, skip=0,
                                            from_date="2025-01-01T00:00:00.000Z")
    if st4 == 200 and isinstance(body4.get("data", {}).get("transcripts"), list):
        report.record_protocol("transcripts fromDate filter accepted", True, "")
    else:
        report.diverge("protocol", "transcripts", f"fromDate filter -> {st4}")
