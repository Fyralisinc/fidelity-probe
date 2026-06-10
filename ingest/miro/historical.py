"""Miro historical ingestion — enumerate boards (OFFSET), then walk items (CURSOR).

The REAL Miro v2 contract (pinned from Miro's published OpenAPI spec): the read
surface splits into TWO DIFFERENT paginators, which a Brex-clone "everything is the
same paginator" consumer gets wrong:

  1. ``GET /v2/boards`` — OFFSET pagination (``limit`` 1-50/def 20 + ``offset``),
     envelope ``{data,total,size,offset,limit,links,type}`` (the offset envelope
     carries a top-level ``type``);
  2. per board ``GET /v2/boards/{id}/items`` — CURSOR pagination (``limit`` 10-50/
     def 10 + opaque ``cursor``), envelope ``{data,total,size,cursor,limit,links}``
     (NO top-level ``type``; the ``cursor`` field is ABSENT on the last page — that
     absence is how EOF is signalled).

The single Miro signal is the board ITEM (sticky note / shape / text / card / frame).
Items have **NO version field** — only ``createdAt``/``modifiedAt`` (UTC ISO-8601 with
millisecond precision + ``Z``); the versioned dedup key
``external_id = miro:{org_id}:item:{item_id}:{version}`` therefore versions on
``modifiedAt`` (an in-place edit → new modifiedAt → a new observation). Board users
(``owner``/``createdBy``/``modifiedBy``) carry ``name``; item users
(``createdBy``/``modifiedBy``) are ``{id, type}`` with NO ``name``.

Miro publishes an OpenAPI spec but we validate structurally here (the wire facts a
consumer depends on) and assert the two walks terminate.
"""
from __future__ import annotations

import re
from typing import Any

from ..fidelity import FidelityReport
from .client import MiroClient

_BOARDS_PAGE = 2     # small, to genuinely exercise the OFFSET multi-page walk
_ITEMS_PAGE = 50     # the documented max — exercises the CURSOR multi-page walk
_MAX_PAGES = 10_000

# ms-precision Z: 2022-03-30T17:26:50.000Z
_MS_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _ok(report: FidelityReport, seen_ok: set, check: str, ok: bool, detail: str = "") -> None:
    if not ok:
        report.record_protocol(check, False, detail)
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def _validate_board(b: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    for k in ("id", "name", "description", "type"):
        if not isinstance(b.get(k), str):
            problems.append(f"required `{k}` must be a string: {b.get(k)!r}")
    if b.get("type") != "board":
        problems.append(f"`type` must be 'board': {b.get('type')!r}")
    for k in ("createdAt", "modifiedAt"):
        ts = b.get(k)
        if not (isinstance(ts, str) and _MS_Z_RE.match(ts)):
            problems.append(f"`{k}` must be ms-precision ISO-8601 Z: {ts!r}")
    team = b.get("team")
    if not (isinstance(team, dict) and isinstance(team.get("id"), str)
            and team.get("type") == "team"):
        problems.append(f"`team` must be {{id,name,type:'team'}}: {team!r}")
    _ok(report, seen_ok, "board object contract (id,name,description,type:'board',"
        "team,createdAt/modifiedAt ms-Z)", not problems,
        f"id={b.get('id')}: " + "; ".join(problems))
    # board-scoped users MUST carry `name`
    owner = b.get("owner")
    if isinstance(owner, dict):
        _ok(report, seen_ok, "board user objects carry `name` (id,name,type:'user')",
            "name" in owner and owner.get("type") == "user",
            f"owner={owner!r}")
    cum = b.get("currentUserMembership")
    if cum is not None:
        _ok(report, seen_ok, "currentUserMembership is a BoardMember {id,name,role,type:"
            "'board_member'}",
            isinstance(cum, dict) and cum.get("type") == "board_member"
            and "role" in cum and "name" in cum, f"currentUserMembership={cum!r}")


def _validate_item(it: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(it.get("id"), str) and it["id"]):
        problems.append(f"required `id` must be a non-empty string: {it.get('id')!r}")
    if not (isinstance(it.get("type"), str) and it["type"]):
        problems.append(f"required `type` must be a non-empty string: {it.get('type')!r}")
    if not isinstance(it.get("data"), dict):
        problems.append(f"`data` must be an object (type-specific): {it.get('data')!r}")
    for k in ("createdAt", "modifiedAt"):
        ts = it.get(k)
        if not (isinstance(ts, str) and _MS_Z_RE.match(ts)):
            problems.append(f"`{k}` must be ms-precision ISO-8601 Z: {ts!r}")
    # items have NO version field — only timestamps
    if "version" in it:
        problems.append("item must NOT carry a `version` field (Miro items are timestamp-only)")
    geom = it.get("geometry")
    if geom is not None and not isinstance(geom, dict):
        problems.append(f"`geometry` must be an object: {geom!r}")
    pos = it.get("position")
    if pos is not None and not isinstance(pos, dict):
        problems.append(f"`position` must be an object: {pos!r}")
    _ok(report, seen_ok, "item object contract (id,type,data,geometry,position,"
        "createdAt/modifiedAt ms-Z, NO version field)", not problems,
        f"id={it.get('id')}: " + "; ".join(problems))
    # item-scoped users carry NO `name` (only {id, type})
    for uk in ("createdBy", "modifiedBy"):
        u = it.get(uk)
        if u is not None:
            _ok(report, seen_ok, "item user objects are {id,type:'user'} with NO `name`",
                isinstance(u, dict) and u.get("type") == "user" and "name" not in u
                and isinstance(u.get("id"), str), f"{uk}={u!r}")


def _item_external_id(org_id: str, it: dict) -> str:
    """The versioned, org-namespaced dedup key. Items have no version field, so the
    version segment falls back to modifiedAt (an in-place edit re-observes)."""
    ver = it.get("modifiedAt") or it.get("createdAt") or "none"
    return f"miro:{org_id}:item:{it['id']}:{ver}"


def run_historical(report: FidelityReport, cfg) -> None:
    client = MiroClient(cfg, report)
    org_id = client.org_id
    report.auth.update({"method": "org-app Bearer token (Authorization: Bearer, scope "
                        "boards:read)"})
    seen_ok: set = set()

    # 1) enumerate boards — OFFSET pagination. Walk via links.next (full URL) at a
    #    small page size to genuinely cross page boundaries.
    boards: list[dict] = []
    status, _, body = client.list_boards(limit=_BOARDS_PAGE, offset=0)
    pages = 0
    walked = 0
    while pages < _MAX_PAGES:
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "boards",
                           f"GET /v2/boards -> {status}; {str(body)[:160]}")
            return
        # The OFFSET envelope carries data/total/size/offset/limit/links/type.
        missing = {"data", "total", "size", "offset", "limit", "links"} - set(body)
        _ok(report, seen_ok, "boards (offset) envelope {data,total,size,offset,limit,"
            "links,type}", not missing and "type" in body,
            f"keys={sorted(body)}")
        data = body.get("data")
        if not isinstance(data, list):
            report.diverge("protocol", "boards", "`data` is not an array")
            return
        pages += 1
        for b in data:
            if isinstance(b, dict):
                _validate_board(b, report, seen_ok)
                boards.append(b)
                walked += 1
        links = body.get("links") or {}
        nxt = links.get("next") if isinstance(links, dict) else None
        report.record_page("boards", str(body.get("offset")))
        if not nxt:
            break
        # the offset paginator's next link carries offset= (NOT a cursor)
        _ok(report, seen_ok, "boards next link is offset-based (carries offset=)",
            "offset=" in nxt, f"next={nxt!r}")
        status, _, body = client.follow(nxt, "boards")
    report.count("miro_board", len(boards))
    report.note(f"enumerated {len(boards)} boards over {pages} OFFSET page(s)")

    if not boards:
        report.diverge("protocol", "boards", "no boards enumerated")
        return

    # 2) single-board probe — GET /v2/boards/{id} adds a links{self,related} object.
    b0 = boards[0]["id"]
    st, _, sb = client.get_board(b0)
    if st == 200 and isinstance(sb, dict):
        links = sb.get("links")
        _ok(report, seen_ok, "single-board GET adds links{self,related}",
            isinstance(links, dict) and "self" in links and "related" in links,
            f"links={links!r}")
    else:
        report.diverge("protocol", "board", f"GET /v2/boards/{b0} -> {st}; {str(sb)[:120]}")

    # 3) per board — items CURSOR walk. The cursor envelope has NO top-level `type`;
    #    `cursor` is present until the last page, then ABSENT (EOF signal).
    external_ids: set = set()
    total_items = 0
    for b in boards:
        bid = b["id"]
        status, _, ib = client.list_items(bid, limit=_ITEMS_PAGE)
        pages = 0
        while pages < _MAX_PAGES:
            if status != 200 or not isinstance(ib, dict):
                report.diverge("protocol", "items",
                               f"GET /v2/boards/{bid}/items -> {status}; {str(ib)[:140]}")
                break
            # cursor envelope: NO top-level `type` (unlike the boards offset one).
            missing = {"data", "total", "size", "limit", "links"} - set(ib)
            _ok(report, seen_ok, "items (cursor) envelope {data,total,size,limit,links} "
                "with NO top-level type", not missing and "type" not in ib,
                f"keys={sorted(ib)}")
            data = ib.get("data")
            if not isinstance(data, list):
                report.diverge("protocol", "items", "`data` is not an array")
                break
            pages += 1
            for it in data:
                if isinstance(it, dict):
                    _validate_item(it, report, seen_ok)
                    total_items += 1
                    eid = _item_external_id(org_id, it)
                    external_ids.add(eid)
            links = ib.get("links") or {}
            nxt = links.get("next") if isinstance(links, dict) else None
            has_cursor = "cursor" in ib
            report.record_page("items", ib.get("cursor"))
            if not nxt:
                # terminal page: the `cursor` field MUST be absent (Miro's EOF signal)
                _ok(report, seen_ok, "items terminal page omits `cursor` (EOF signal)",
                    not has_cursor, f"cursor still present on the last page of {bid}")
                break
            _ok(report, seen_ok, "items non-terminal page carries `cursor`",
                has_cursor, f"no cursor on a page with links.next ({bid})")
            status, _, ib = client.follow(nxt, "items")

    report.count("miro_item", total_items)

    # 4) the item observation stream — versioned, org-namespaced, deduped.
    unique_ok = len(external_ids) == total_items
    _ok(report, seen_ok, "item external_id miro:{org}:item:{id}:{version} is unique "
        "across the backfill", unique_ok,
        f"{len(external_ids)} ids for {total_items} items")
    report.note(f"item stream: {total_items} items across {len(boards)} boards "
                f"({len(external_ids)} unique external_ids; version segment = modifiedAt "
                f"since Miro items have no version field)")
    report.note("Miro is POLL-ONLY: experimental webhooks were discontinued 2025-12-05, "
                "so incremental ingestion is re-walking /items and dedup'ing on the "
                "versioned external_id — there is no live push to verify.")
