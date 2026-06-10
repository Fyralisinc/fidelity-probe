"""Carta historical ingestion — the issuer cap-table backfill.

The REAL Carta contract (docs.carta.com, Google-AIP conventions):
  * Reads are REST collections under ``/v1alpha1/issuers/{issuerId}/…`` with Google
    **AIP-158 token pagination** — ``pageSize`` (default 25, per-endpoint max) +
    opaque ``pageToken`` → the response wraps the list under its PLURAL key
    alongside ``nextPageToken``; the ``nextPageToken`` field is ABSENT on the last
    page (that absence is the EOF signal). Single-object GETs wrap under a SINGULAR
    key (``{issuer:{…}}``).
  * MONEY + every decimal/quantity is a PROTOBUF WRAPPER whose ``value`` is a decimal
    STRING — Money = ``{currencyCode:{value:"USD"}, amount:{value:"<dec>"}}``; a bare
    decimal/quantity = ``{value:"<dec>"}``. NOT a number, NOT integer cents.
  * IDs are MIXED: the issuer suite uses SHORT NUMERIC-STRING ids ("611"); the
    cross-ref ``securityId``/``shareClassId`` are UUIDs.
  * Timestamps are RFC3339 UTC with ``Z`` + microseconds; pure dates are ``YYYY-MM-DD``.
  * There is NO SyncToken anywhere (a QBO-archetype field the Fyralis client expects);
    the securities carry ``lastModifiedDatetime`` to version on instead.

Carta publishes no single machine schema (the reference pages are generated from an
unpublished OpenAPI), so — like the QBO/ramp/miro slices — we structurally validate
the fields a consumer depends on and assert the AIP token walks terminate. The four
collections are the Fyralis "cap-table signal set" (shareholders, share classes,
SAFE notes, option grants).
"""
from __future__ import annotations

import re
from typing import Any

from ..fidelity import FidelityReport
from .client import CartaClient

_PAGE = 2            # small, to genuinely exercise the multi-page AIP token walk
_MAX_PAGES = 10_000

_US_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DEC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NUMSTR_RE = re.compile(r"^\d+$")

_RELATIONSHIPS = {
    "ADVISOR", "EX_ADVISOR", "BOARD_MEMBER", "CONSULTANT", "EX_CONSULTANT",
    "EMPLOYEE", "EX_EMPLOYEE", "EXECUTIVE", "FOUNDER", "INTERNATIONAL_EMPLOYEE",
    "INVESTOR", "OFFICER", "OTHER", "EX_BOARD_MEMBER", "EX_INTERNATIONAL_EMPLOYEE",
}
_ENTITY_TYPES = {
    "INDIVIDUAL", "CORPORATION", "LIMITED_LIABILITY_CORPORATION", "ESTATE_OR_TRUST",
    "PARTNERSHIP", "DISREGARDED_ENTITY", "UNKNOWN",
}
_STOCK_OPTION_TYPES = {"ISO", "NSO", "OTHER"}


def _ok(report: FidelityReport, seen_ok: set, check: str, ok: bool, detail: str = "") -> None:
    if not ok:
        report.record_protocol(check, False, detail)
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def _is_decimal_wrapper(v: Any) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("value"), str)
            and bool(_DEC_RE.match(v["value"])))


def _is_money(v: Any, *, nullable_ok: bool = False) -> bool:
    if v is None:
        return nullable_ok
    return (isinstance(v, dict)
            and isinstance(v.get("currencyCode"), dict)
            and isinstance(v["currencyCode"].get("value"), str)
            and isinstance(v.get("amount"), dict)
            and isinstance(v["amount"].get("value"), str)
            and bool(_DEC_RE.match(v["amount"]["value"])))


# --------------------------------------------------------------- per-entity checks

def _validate_stakeholder(sh: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(sh.get("id"), str) and _NUMSTR_RE.match(sh["id"])):
        problems.append(f"`id` must be a numeric STRING: {sh.get('id')!r}")
    if not isinstance(sh.get("fullName"), str):
        problems.append("`fullName` must be a string")
    if sh.get("relationship") not in _RELATIONSHIPS:
        problems.append(f"relationship not in enum: {sh.get('relationship')!r}")
    if sh.get("entityType") not in _ENTITY_TYPES:
        problems.append(f"entityType not in enum: {sh.get('entityType')!r}")
    addr = sh.get("address")
    if not (isinstance(addr, dict) and "country" in addr):
        problems.append(f"`address` must be an object with country: {addr!r}")
    _ok(report, seen_ok, "stakeholder contract (numeric-string id, fullName, "
        "relationship/entityType enums, address.country)", not problems,
        f"id={sh.get('id')}: " + "; ".join(problems))


def _validate_share_class(sc: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(sc.get("id"), str) and _NUMSTR_RE.match(sc["id"])):
        problems.append(f"`id` must be a numeric STRING: {sc.get('id')!r}")
    if sc.get("type") not in {"COMMON", "PREFERRED"}:
        problems.append(f"type not in {{COMMON,PREFERRED}}: {sc.get('type')!r}")
    if not _is_decimal_wrapper(sc.get("authorizedShareCount")):
        problems.append("`authorizedShareCount` must be a decimal wrapper {value:'<str>'}")
    if not _is_money(sc.get("parValue")):
        problems.append("`parValue` must be a Money wrapper {currencyCode:{value},amount:{value}}")
    if not isinstance(sc.get("seniority"), int) or isinstance(sc.get("seniority"), bool):
        problems.append("`seniority` must be an int")
    if not isinstance(sc.get("pariPassu"), bool):
        problems.append("`pariPassu` must be a bool")
    _ok(report, seen_ok, "shareClass contract (type enum, authorizedShareCount decimal "
        "wrapper, parValue Money wrapper, seniority int, pariPassu bool)",
        not problems, f"id={sc.get('id')}: " + "; ".join(problems))


def _validate_option_grant(g: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(g.get("id"), str) and _NUMSTR_RE.match(g["id"])):
        problems.append(f"`id` must be a numeric STRING: {g.get('id')!r}")
    # mixed ids: numeric-string grant id, UUID securityId.
    if not (isinstance(g.get("securityId"), str) and _UUID_RE.match(g["securityId"])):
        problems.append(f"`securityId` must be a UUID: {g.get('securityId')!r}")
    if g.get("stockOptionType") not in _STOCK_OPTION_TYPES:
        problems.append(f"stockOptionType not in enum: {g.get('stockOptionType')!r}")
    if not _is_decimal_wrapper(g.get("quantity")):
        problems.append("`quantity` must be a decimal wrapper")
    if not _is_money(g.get("exercisePrice")):
        problems.append("`exercisePrice` must be a Money wrapper")
    if not (isinstance(g.get("issueDate"), str) and _DATE_RE.match(g["issueDate"])):
        problems.append(f"`issueDate` must be DATE-only: {g.get('issueDate')!r}")
    if not (isinstance(g.get("lastModifiedDatetime"), str)
            and _US_Z_RE.match(g["lastModifiedDatetime"])):
        problems.append(f"`lastModifiedDatetime` must be RFC3339-µs-Z: "
                        f"{g.get('lastModifiedDatetime')!r}")
    # NO SyncToken (a QBO carryover); NO grant-level status (status is on exercises).
    if "syncToken" in g or "SyncToken" in g:
        problems.append("option grant must NOT carry a SyncToken (Carta has none)")
    _ok(report, seen_ok, "optionGrant contract (numeric-string id + UUID securityId, "
        "stockOptionType enum, quantity decimal wrapper, exercisePrice Money, issueDate "
        "DATE-only, lastModifiedDatetime µs-Z, NO SyncToken)", not problems,
        f"id={g.get('id')}: " + "; ".join(problems))


def _validate_convertible_note(n: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(n.get("id"), str) and _NUMSTR_RE.match(n["id"])):
        problems.append(f"`id` must be a numeric STRING: {n.get('id')!r}")
    if not (isinstance(n.get("securityId"), str) and _UUID_RE.match(n["securityId"])):
        problems.append(f"`securityId` must be a UUID: {n.get('securityId')!r}")
    if not _is_money(n.get("cashPaid")):
        problems.append("`cashPaid` must be a Money wrapper")
    if not _is_money(n.get("priceCap"), nullable_ok=True):
        problems.append("`priceCap` must be a Money wrapper|null")
    if not _is_decimal_wrapper(n.get("interestRate")):
        problems.append("`interestRate` must be a decimal wrapper")
    if not (isinstance(n.get("issueDatetime"), str) and _US_Z_RE.match(n["issueDatetime"])):
        problems.append(f"`issueDatetime` must be RFC3339-µs-Z: {n.get('issueDatetime')!r}")
    _ok(report, seen_ok, "convertibleNote/SAFE contract (numeric-string id + UUID "
        "securityId, cashPaid/priceCap Money, interestRate decimal wrapper, issueDatetime "
        "µs-Z)", not problems, f"id={n.get('id')}: " + "; ".join(problems))


# ----------------------------------------------------------------- the AIP walker

def _walk_collection(client: CartaClient, collection: str, validate,
                     report: FidelityReport, count_key: str, **filters) -> tuple[int, int]:
    """Walk an AIP-token-paginated collection; validate the envelope + each object.

    Returns (count, pages). The envelope is ``{<collection>:[…], nextPageToken?}`` and
    ``nextPageToken`` is ABSENT on the terminal page (the EOF signal)."""
    seen_ok: set = set()
    external_ids: set = set()
    status, _, body = client.list_collection(collection, page_size=_PAGE, **filters)
    report.record_page(collection, "head")
    walked, pages = 0, 0
    while pages < _MAX_PAGES:
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", collection,
                           f"GET .../{collection} -> {status}; {str(body)[:160]}")
            return walked, pages
        data = body.get(collection)
        if not isinstance(data, list):
            report.diverge("protocol", collection,
                           f"list must wrap under the plural key `{collection}`; "
                           f"keys={sorted(body)}")
            return walked, pages
        _ok(report, seen_ok, f"{collection} AIP envelope {{{collection}:[…], "
            "nextPageToken?}}", set(body) - {collection, "nextPageToken"} == set(),
            f"unexpected keys={sorted(set(body) - {collection, 'nextPageToken'})}")
        pages += 1
        for obj in data:
            if isinstance(obj, dict):
                report.count(count_key)
                validate(obj, report, seen_ok)
                walked += 1
                ver = obj.get("lastModifiedDatetime") or "none"
                external_ids.add(f"carta:{client.issuer_id}:{count_key}:{obj.get('id')}:{ver}")
        nxt = body.get("nextPageToken")
        has_token = "nextPageToken" in body
        if not nxt:
            # terminal page: nextPageToken MUST be absent (the AIP EOF signal).
            _ok(report, seen_ok, f"{collection} terminal page OMITS nextPageToken "
                "(AIP EOF signal)", not has_token,
                "nextPageToken still present on the last page")
            break
        _ok(report, seen_ok, f"{collection} non-terminal page carries nextPageToken",
            has_token, "no nextPageToken on a page that has more")
        status, _, body = client.list_collection(collection, page_size=_PAGE,
                                                  page_token=nxt, **filters)
        report.record_page(collection, nxt)
    _ok(report, seen_ok, f"{collection} external_id "
        f"carta:{{issuer}}:{count_key}:{{id}}:{{version}} unique across the backfill",
        len(external_ids) == walked, f"{len(external_ids)} ids for {walked} objects")
    return walked, pages


def run_historical(report: FidelityReport, cfg) -> None:
    client = CartaClient(cfg, report)
    report.auth.update({"method": "OAuth client-credentials -> Bearer (Carta v1alpha1; "
                        "no refresh_token — re-mint ~1h)"})
    seen_ok: set = set()

    # 0) issuers list (plural key) + single-issuer GET (singular {issuer:{…}} wrap).
    st, _, lb = client.list_issuers()
    if st == 200 and isinstance(lb, dict) and isinstance(lb.get("issuers"), list):
        report.record_protocol("GET /v1alpha1/issuers wraps under plural `issuers`", True, "")
        for iss in lb["issuers"]:
            if isinstance(iss, dict):
                report.count("issuer")
                _ok(report, seen_ok, "issuer id is a numeric STRING",
                    isinstance(iss.get("id"), str) and bool(_NUMSTR_RE.match(iss["id"])),
                    f"id={iss.get('id')!r}")
    else:
        report.diverge("protocol", "issuers", f"GET /v1alpha1/issuers -> {st}; {str(lb)[:120]}")

    st, _, sb = client.get_issuer()
    if st == 200 and isinstance(sb, dict):
        _ok(report, seen_ok, "single-issuer GET wraps under the SINGULAR key {issuer:{…}}",
            set(sb) == {"issuer"} and isinstance(sb["issuer"], dict),
            f"keys={sorted(sb)}")
    else:
        report.diverge("protocol", "issuer", f"GET /v1alpha1/issuers/{{id}} -> {st}")

    # 1) the four cap-table collections — each an AIP token walk.
    n_sh, p_sh = _walk_collection(client, "stakeholders", _validate_stakeholder,
                                  report, "stakeholder")
    report.note(f"stakeholders: {n_sh} over {p_sh} AIP page(s)")
    n_sc, _ = _walk_collection(client, "shareClasses", _validate_share_class,
                               report, "shareClass")
    report.note(f"shareClasses: {n_sc}")
    n_g, p_g = _walk_collection(client, "optionGrants", _validate_option_grant,
                                report, "optionGrant")
    report.note(f"optionGrants: {n_g} over {p_g} AIP page(s) (the primary stream)")
    n_n, _ = _walk_collection(client, "convertibleNotes", _validate_convertible_note,
                              report, "convertibleNote")
    report.note(f"convertibleNotes (SAFEs): {n_n}")

    # 2) the lastModifiedDatetimeAfter incremental knob (informational probe).
    st, _, body = client.list_collection("optionGrants", page_size=_PAGE,
                                         lastModifiedDatetimeAfter="2099-01-01T00:00:00Z")
    if st == 200 and isinstance(body, dict):
        report.record_protocol("optionGrants lastModifiedDatetimeAfter filter accepted",
                               True, "")
        _ok(report, seen_ok, "lastModifiedDatetimeAfter far-future yields an empty page",
            body.get("optionGrants") == [], f"got {len(body.get('optionGrants') or [])}")

    report.note("Carta is POLL-ONLY: it has NO webhook / push of any kind, so incremental "
                "ingestion is re-walking the collections (optionally with "
                "lastModifiedDatetimeAfter) and dedup'ing on the versioned external_id "
                "carta:{issuer}:{kind}:{id}:{version} (version = lastModifiedDatetime where "
                "present; Carta has NO SyncToken — a QBO-archetype field the Fyralis client "
                "expects). There is no live push to verify.")
