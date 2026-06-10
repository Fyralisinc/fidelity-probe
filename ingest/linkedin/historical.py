"""LinkedIn historical ingestion — the organization-page backfill.

The REAL LinkedIn contract (learn.microsoft.com/linkedin, Community-Management API):
  * Reads are Rest.li FINDER collections under ``/rest/`` scoped by an organization
    URN query param. ``GET /rest/posts?q=author&author={orgURN}`` pages by OFFSET
    (``start``/``count``, default 10 / max 100); the envelope is
    ``{elements:[…], paging:{start,count,links:[…]}}`` and the EOF signal is a page
    with FEWER elements than ``count`` (LinkedIn's documented "you have reached the
    end when your response contains fewer elements than your count" rule).
  * A post ``id`` is a URN (``urn:li:share:{n}`` or ``urn:li:ugcPost:{n}``); ``author``
    is the org URN; ``createdAt``/``lastModifiedAt``/``publishedAt`` are epoch-MILLIS
    INTEGERS (NOT ISO strings).
  * The two stats finders (``q=organizationalEntity``) return a SINGLE lifetime
    ``elements`` row — ``totalShareStatistics`` (seven counters + engagement) and the
    follower facet arrays (``{<segmentKey>, followerCounts:{organicFollowerCount,
    paidFollowerCount}}``; no lifetime total on this endpoint).

LinkedIn publishes no single machine schema for these endpoints (the docs are prose +
samples), so — like the QBO/carta/miro slices — we structurally validate the fields a
consumer depends on and assert the OFFSET walk terminates on the documented short
page. The three families are the Fyralis organization signal set (it calls them
share / social_action / follower_stat).
"""
from __future__ import annotations

import re
from typing import Any

from ..fidelity import FidelityReport
from .client import LinkedinClient

_PAGE = 2            # small, to genuinely exercise the multi-page OFFSET walk
_MAX_PAGES = 10_000

_POST_URN_RE = re.compile(r"^urn:li:(share|ugcPost):\d+$")
_ORG_URN_RE = re.compile(r"^urn:li:organization:\d+$")

_SHARE_FIELDS = {"clickCount", "commentCount", "engagement", "impressionCount",
                 "likeCount", "shareCount", "uniqueImpressionsCount"}
_FOLLOWER_FACETS = {
    "followerCountsByAssociationType", "followerCountsBySeniority",
    "followerCountsByFunction", "followerCountsByStaffCountRange",
    "followerCountsByGeoCountry", "followerCountsByGeo", "followerCountsByIndustry",
}


def _ok(report: FidelityReport, seen_ok: set, check: str, ok: bool, detail: str = "") -> None:
    if not ok:
        report.record_protocol(check, False, detail)
    elif check not in seen_ok:
        seen_ok.add(check)
        report.record_protocol(check, True, "")


def _is_epoch_ms(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 1_000_000_000_000


# ----------------------------------------------------------------- per-entity checks

def _validate_post(p: dict, org_urn: str, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(p.get("id"), str) and _POST_URN_RE.match(p["id"])):
        problems.append(f"`id` must be a share/ugcPost URN: {p.get('id')!r}")
    if p.get("author") != org_urn:
        problems.append(f"`author` must be the org URN {org_urn}: {p.get('author')!r}")
    if not isinstance(p.get("commentary"), str):
        problems.append("`commentary` must be a string")
    if p.get("lifecycleState") not in {"PUBLISHED", "DRAFT", "PUBLISH_REQUESTED",
                                       "PUBLISH_FAILED", "PROCESSING"}:
        problems.append(f"lifecycleState not in enum: {p.get('lifecycleState')!r}")
    for k in ("createdAt", "lastModifiedAt", "publishedAt"):
        if not _is_epoch_ms(p.get(k)):
            problems.append(f"`{k}` must be an epoch-MILLIS integer: {p.get(k)!r}")
    dist = p.get("distribution")
    if not (isinstance(dist, dict) and "feedDistribution" in dist):
        problems.append(f"`distribution` must carry feedDistribution: {dist!r}")
    lsi = p.get("lifecycleStateInfo")
    if not (isinstance(lsi, dict) and isinstance(lsi.get("isEditedByAuthor"), bool)):
        problems.append("`lifecycleStateInfo.isEditedByAuthor` must be a bool")
    _ok(report, seen_ok, "post contract (share/ugcPost URN id, org-URN author, "
        "epoch-MILLIS createdAt/lastModifiedAt/publishedAt, lifecycleState enum, "
        "distribution+lifecycleStateInfo)", not problems,
        f"id={p.get('id')}: " + "; ".join(problems))


def _validate_share_stats(el: dict, org_urn: str, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if el.get("organizationalEntity") != org_urn:
        problems.append(f"organizationalEntity must echo {org_urn}: "
                        f"{el.get('organizationalEntity')!r}")
    tss = el.get("totalShareStatistics")
    if not isinstance(tss, dict):
        problems.append("`totalShareStatistics` must be an object")
    else:
        if set(tss) != _SHARE_FIELDS:
            problems.append(f"totalShareStatistics fields {sorted(set(tss))} != "
                            f"the seven documented {sorted(_SHARE_FIELDS)}")
        if not isinstance(tss.get("engagement"), (int, float)) or isinstance(
                tss.get("engagement"), bool):
            problems.append("`engagement` must be a number")
        for k in _SHARE_FIELDS - {"engagement"}:
            if not isinstance(tss.get(k), int) or isinstance(tss.get(k), bool):
                problems.append(f"`{k}` must be an int")
    _ok(report, seen_ok, "shareStatistics contract (lifetime totalShareStatistics with "
        "exactly the seven counters + engagement; organizationalEntity echoed)",
        not problems, "; ".join(problems))


def _validate_follower_stats(el: dict, org_urn: str, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if el.get("organizationalEntity") != org_urn:
        problems.append(f"organizationalEntity must echo {org_urn}")
    missing = _FOLLOWER_FACETS - set(el)
    if missing:
        problems.append(f"missing facet arrays: {sorted(missing)}")
    # a representative segment shape
    arr = el.get("followerCountsByAssociationType")
    if isinstance(arr, list) and arr:
        seg = arr[0]
        fc = seg.get("followerCounts") if isinstance(seg, dict) else None
        if not (isinstance(fc, dict) and "organicFollowerCount" in fc
                and "paidFollowerCount" in fc):
            problems.append("facet segment must carry followerCounts{organic,paid}")
        if seg.get("associationType") not in {"EMPLOYEE", "MEMBER"}:
            problems.append(f"associationType enum: {seg.get('associationType')!r}")
    # the lifetime endpoint has NO total (removed — use networkSizes).
    if "totalFollowerCounts" in el or "firstDegreeSize" in el:
        problems.append("follower stats must NOT carry a lifetime total on this endpoint")
    _ok(report, seen_ok, "followerStatistics contract (seven facet arrays of "
        "{<segmentKey>, followerCounts{organic,paid}}; NO lifetime total)",
        not problems, "; ".join(problems))


# ----------------------------------------------------------------- the OFFSET walker

def _walk_posts(client: LinkedinClient, report: FidelityReport) -> tuple[int, int]:
    """Walk the OFFSET-paginated posts finder; validate the envelope + each element.

    Returns (count, pages). The envelope is ``{elements, paging:{start,count,links}}``
    and the EOF signal is a page with fewer elements than ``count``."""
    seen_ok: set = set()
    external_ids: set = set()
    org_urn = client.organization_urn
    start, walked, pages = 0, 0, 0
    while pages < _MAX_PAGES:
        status, _, body = client.list_posts(start=start, count=_PAGE)
        report.record_page("posts", str(start))
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "posts",
                           f"GET /rest/posts start={start} -> {status}; {str(body)[:160]}")
            return walked, pages
        els = body.get("elements")
        paging = body.get("paging")
        if not isinstance(els, list) or not isinstance(paging, dict):
            report.diverge("protocol", "posts",
                           f"posts envelope must be {{elements,paging}}; keys={sorted(body)}")
            return walked, pages
        _ok(report, seen_ok, "posts Rest.li FINDER envelope {elements:[…], "
            "paging:{start,count,links}}",
            {"start", "count"} <= set(paging) and isinstance(paging.get("links", []), list)
            and paging.get("start") == start,
            f"paging={paging}")
        pages += 1
        for p in els:
            if isinstance(p, dict):
                report.count("post")
                _validate_post(p, org_urn, report, seen_ok)
                walked += 1
                external_ids.add(f"linkedin:{org_urn}:share:{p.get('id')}")
        if len(els) < _PAGE:
            # the documented EOF signal: a page with fewer elements than `count`.
            _ok(report, seen_ok, "posts OFFSET walk terminates on a short page "
                "(< count elements = EOF)", True, "")
            break
        start += _PAGE
    _ok(report, seen_ok, "posts external_id linkedin:{org}:share:{urn} unique across "
        "the backfill", len(external_ids) == walked,
        f"{len(external_ids)} ids for {walked} posts")
    return walked, pages


def run_historical(report: FidelityReport, cfg) -> None:
    client = LinkedinClient(cfg, report)
    report.auth.update({"method": "OAuth Bearer (3-legged member auth) + Linkedin-Version "
                        "+ X-Restli-Protocol-Version:2.0.0"})
    seen_ok: set = set()
    org_urn = client.organization_urn

    # 0) organization lookup probe (connectivity / token-verify).
    m = _ORG_URN_RE.match(org_urn or "")
    org_id = org_urn.rsplit(":", 1)[-1] if m else org_urn
    st, _, ob = client.get_organization(org_id)
    if st == 200 and isinstance(ob, dict):
        report.count("organization")
        _ok(report, seen_ok, "GET /rest/organizations/{id}: id is a bare INT + carries "
            "vanityName/localizedName/name",
            isinstance(ob.get("id"), int) and "vanityName" in ob
            and "localizedName" in ob and isinstance(ob.get("name"), dict),
            f"org={ {k: ob.get(k) for k in ('id','vanityName')} }")
    else:
        report.diverge("protocol", "organization",
                       f"GET /rest/organizations/{org_id} -> {st}; {str(ob)[:120]}")

    # 1) POSTS — the primary OFFSET-paginated stream (the `share` family).
    n_posts, p_posts = _walk_posts(client, report)
    report.note(f"posts: {n_posts} over {p_posts} OFFSET page(s) (the share stream)")

    # 2) SHARE STATISTICS — the lifetime totalShareStatistics (the `social_action` family).
    st, _, sb = client.share_statistics()
    if st == 200 and isinstance(sb, dict) and isinstance(sb.get("elements"), list):
        _ok(report, seen_ok, "shareStatistics is a single lifetime elements row",
            len(sb["elements"]) == 1, f"{len(sb['elements'])} elements")
        for el in sb["elements"]:
            if isinstance(el, dict):
                report.count("shareStatistics")
                _validate_share_stats(el, org_urn, report, seen_ok)
        report.note("shareStatistics: lifetime totalShareStatistics aggregate")
    else:
        report.diverge("protocol", "shareStatistics",
                       f"GET /rest/organizationalEntityShareStatistics -> {st}")

    # 3) FOLLOWER STATISTICS — the lifetime facet breakdowns (the `follower_stat` family).
    st, _, fb = client.follower_statistics()
    if st == 200 and isinstance(fb, dict) and isinstance(fb.get("elements"), list):
        _ok(report, seen_ok, "followerStatistics is a single lifetime elements row",
            len(fb["elements"]) == 1, f"{len(fb['elements'])} elements")
        for el in fb["elements"]:
            if isinstance(el, dict):
                report.count("followerStatistics")
                _validate_follower_stats(el, org_urn, report, seen_ok)
        report.note("followerStatistics: lifetime facet breakdowns (no lifetime total)")
    else:
        report.diverge("protocol", "followerStatistics",
                       f"GET /rest/organizationalEntityFollowerStatistics -> {st}")

    report.note("LinkedIn org data is POLL-ONLY: it has NO webhook / push of any kind "
                "(partner-gated, no webhook entitlement; absent from the VERIFIERS "
                "registry), so incremental ingestion is re-walking posts (optionally "
                "newest-first via sortBy=LAST_MODIFIED) + re-reading the lifetime stats "
                "and dedup'ing on the entity-kind-discriminated external_id "
                "linkedin:{org}:{kind}:{id}. There is no live push to verify.")
