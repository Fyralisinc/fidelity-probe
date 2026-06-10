"""Ashby historical ingestion — cursor-paged ``.list`` per entity category.

Fyralis shards Ashby one entity-type per shard; we mirror that: for each of
``candidate``, ``application``, ``job``, ``interview``, ``offer`` we walk
``POST /<category>.list`` by the opaque ``nextCursor`` until ``moreDataAvailable``
is false, then capture the minted ``syncToken`` and re-list with it to confirm an
unchanged incremental sync returns no rows. Ashby publishes an OpenAPI but no
machine-validatable JSON-Schema per object, so — like the QBO/Grafana/Mercury
slices — we structurally validate the fields a consumer depends on (incl. the
documented enums) and assert the envelope + cursor pagination terminate.

Envelope facts asserted (developer.ashbyhq.com): ``results`` is an ARRAY for
``.list`` / an OBJECT for ``.info``; ``nextCursor`` is present ONLY while
``moreDataAvailable`` is true; ``syncToken`` is returned ONLY on the terminal
page. Business errors are HTTP 200 + ``success:false``.
"""
from __future__ import annotations

from typing import Any

from ..fidelity import FidelityReport
from .client import AshbyClient

_CATEGORIES = ("candidate", "application", "job", "interview", "offer")
_PAGE = 100
_MAX_PAGES = 10_000

_APPLICATION_STATUSES = {"Hired", "Archived", "Active", "Lead"}
_JOB_STATUSES = {"Draft", "Open", "Closed", "Archived"}
_EMPLOYMENT_TYPES = {"FullTime", "PartTime", "Intern", "Contract", "Temporary"}
_OFFER_ACCEPTANCE = {"Accepted", "Declined", "Pending", "Created", "Cancelled",
                     "WaitingOnResponse"}
_OFFER_STATUS = {"WaitingOnApprovalStart", "WaitingOnOfferApproval",
                 "WaitingOnApprovalDefinition", "WaitingOnCandidateResponse",
                 "CandidateRejected", "CandidateAccepted", "OfferCancelled"}


def _rfc3339_z(x: Any) -> bool:
    # Ashby timestamps are ISO-8601 UTC + Z (millis preferred, seconds also valid).
    return isinstance(x, str) and x.endswith("Z") and "T" in x


def _req(obj: dict, keys, problems: list[str]) -> None:
    for k in keys:
        if obj.get(k) in (None, ""):
            problems.append(f"missing/empty `{k}`")


def _validate(category: str, e: dict, problems: list[str]) -> None:
    if not isinstance(e, dict):
        problems.append("entity is not an object")
        return
    if not e.get("id"):
        problems.append("missing `id`")
    if category == "candidate":
        _req(e, ("name", "profileUrl"), problems)
        for k in ("createdAt", "updatedAt"):
            if not _rfc3339_z(e.get(k)):
                problems.append(f"`{k}` must be RFC3339 UTC Z: {e.get(k)!r}")
        for arr in ("emailAddresses", "phoneNumbers", "socialLinks", "tags",
                    "applicationIds"):
            if not isinstance(e.get(arr), list):
                problems.append(f"`{arr}` must be an array")
    elif category == "application":
        for k in ("createdAt", "updatedAt"):
            if not _rfc3339_z(e.get(k)):
                problems.append(f"`{k}` must be RFC3339 UTC Z: {e.get(k)!r}")
        if e.get("status") not in _APPLICATION_STATUSES:
            problems.append(f"status not in enum: {e.get('status')!r}")
        if not isinstance(e.get("candidate"), dict):
            problems.append("`candidate` must be a nested object (not a bare id)")
        if not isinstance(e.get("job"), dict):
            problems.append("`job` must be a nested object (not a bare id)")
        if not isinstance(e.get("currentInterviewStage"), dict):
            problems.append("`currentInterviewStage` must be an object")
        if not isinstance(e.get("hiringTeam"), list):
            problems.append("`hiringTeam` must be an array")
    elif category == "job":
        _req(e, ("title",), problems)
        for k in ("createdAt", "updatedAt"):
            if not _rfc3339_z(e.get(k)):
                problems.append(f"`{k}` must be RFC3339 UTC Z: {e.get(k)!r}")
        if e.get("status") not in _JOB_STATUSES:
            problems.append(f"status not in enum: {e.get('status')!r}")
        if e.get("employmentType") not in _EMPLOYMENT_TYPES:
            problems.append(f"employmentType not in enum: {e.get('employmentType')!r}")
        if not isinstance(e.get("confidential"), bool):
            problems.append("`confidential` must be a boolean")
    elif category == "interview":
        _req(e, ("title", "jobId", "feedbackFormDefinitionId"), problems)
        for b in ("isArchived", "isFeedbackRequired"):
            if not isinstance(e.get(b), bool):
                problems.append(f"`{b}` must be a boolean")
    elif category == "offer":
        _req(e, ("applicationId",), problems)
        if e.get("acceptanceStatus") not in _OFFER_ACCEPTANCE:
            problems.append(f"acceptanceStatus not in enum: {e.get('acceptanceStatus')!r}")
        if e.get("offerStatus") not in _OFFER_STATUS:
            problems.append(f"offerStatus not in enum: {e.get('offerStatus')!r}")
        if not isinstance(e.get("latestVersion"), dict):
            problems.append("`latestVersion` must be an object")


def _walk(client: AshbyClient, category: str, report: FidelityReport) -> tuple[list[str], str | None]:
    """Walk one category's .list by cursor; return (ids, terminal syncToken)."""
    seen_ok: set = set()
    ids: list[str] = []
    cursor: str | None = None
    sync_token: str | None = None
    pages = 0
    env_check = f"{category}.list envelope (results[]/moreDataAvailable/cursor/syncToken)"
    obj_check = f"{category} object contract"
    env_ok = True
    while pages < _MAX_PAGES:
        status, _, body = client.list_entities(category, cursor=cursor, limit=_PAGE)
        report.record_page(f"{category}.list", cursor or "head")
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", category, f"{category}.list -> {status}; {str(body)[:160]}")
            return ids, None
        if body.get("success") is not True:
            report.diverge("protocol", category,
                           f"{category}.list success != true: {str(body)[:160]}")
            return ids, None
        results = body.get("results")
        if not isinstance(results, list):
            report.record_protocol(env_check, False, "`results` is not an array")
            return ids, None
        more = body.get("moreDataAvailable")
        if not isinstance(more, bool):
            env_ok = False
            report.record_protocol(env_check, False, f"moreDataAvailable not bool: {more!r}")
        # nextCursor only while more; syncToken only on the terminal page.
        if more and not body.get("nextCursor"):
            env_ok = False
            report.record_protocol(env_check, False, "moreDataAvailable but no nextCursor")
        if not more and body.get("nextCursor"):
            env_ok = False
            report.record_protocol(env_check, False, "terminal page still carries nextCursor")
        if not more:
            sync_token = body.get("syncToken")
            if not sync_token:
                env_ok = False
                report.record_protocol(env_check, False, "terminal page mints no syncToken")
        pages += 1
        for e in results:
            report.count(category)
            problems: list[str] = []
            _validate(category, e, problems)
            if problems:
                report.record_protocol(obj_check, False,
                                       f"id={e.get('id') if isinstance(e, dict) else '?'}: "
                                       + "; ".join(problems))
            elif obj_check not in seen_ok:
                seen_ok.add(obj_check)
                report.record_protocol(obj_check, True, "")
            if isinstance(e, dict) and e.get("id"):
                ids.append(e["id"])
        if not more:
            break
        cursor = body["nextCursor"]
    if env_ok:
        report.record_protocol(env_check, True, "")
    report.note(f"{category}: {len(ids)} over {pages} page(s)")
    return ids, sync_token


def run_historical(report: FidelityReport, cfg) -> None:
    client = AshbyClient(cfg, report)
    report.auth.update({"method": "API key as HTTP Basic username, empty password "
                        "(Authorization: Basic base64(\"<key>:\"))"})

    total = 0
    for category in _CATEGORIES:
        ids, sync_token = _walk(client, category, report)
        total += len(ids)

        # syncToken round-trip: an unchanged incremental sync returns no rows.
        if sync_token:
            st, _, body = client.list_entities(category, sync_token=sync_token, limit=_PAGE)
            check = f"{category} syncToken incremental (unchanged -> empty)"
            if (st == 200 and isinstance(body, dict) and body.get("success") is True
                    and isinstance(body.get("results"), list)):
                if body["results"]:
                    report.record_protocol(check, False,
                                           f"re-sync returned {len(body['results'])} rows "
                                           f"(expected 0 — nothing changed)")
                else:
                    report.record_protocol(check, True, "")
            else:
                report.record_protocol(check, False, f"re-sync -> {st}; {str(body)[:120]}")

        # .info returns a single OBJECT (not an array).
        if ids:
            st, _, body = client.get_entity(category, ids[0])
            check = f"{category}.info returns success + a single results OBJECT"
            if (st == 200 and isinstance(body, dict) and body.get("success") is True
                    and isinstance(body.get("results"), dict)
                    and body["results"].get("id") == ids[0]):
                report.record_protocol(check, True, "")
            else:
                report.record_protocol(check, False, f"{category}.info -> {st}; {str(body)[:160]}")

    report.note(f"ashby: {total} entities across {len(_CATEGORIES)} categories")
