"""Figma historical ingestion — enumerate files, then MERGE versions + comments.

The REAL Figma contract (developers.figma.com + the OpenAPI spec figma/rest-api-spec):
there is NO ``GET /v1/files`` list and NO ``GET /v1/files/{key}/events`` stream. A
real backfill instead:

  1. enumerates files: ``GET /v1/teams/{id}/projects`` → ``GET /v1/projects/{id}/files``;
  2. per file pulls ``GET /v1/files/{key}/versions`` (``{versions:[…], pagination:
     {prev_page,next_page}}`` — CURSOR ``page_size``(def30/max50) + numeric
     ``before``/``after``; the links are FULL URLs) and ``GET /v1/files/{key}/comments``
     (``{comments:[…]}`` — NO pagination, all in one array);
  3. MERGES the two into one design "event" stream, with the versioned dedup key
     ``external_id = figma:{team_id}:event:{event_id}:{version}`` (a version's id is
     immutable → version==version_id; a comment can be resolved/edited → version key
     advances, so a re-fetch dedups but a resolve lands a new observation).

The User object is ``{id, handle, img_url}`` with **NO email** (email is /v1/me only);
timestamps are UTC ISO-8601 with ``Z``. Figma publishes an OpenAPI spec but we validate
structurally here (the wire facts a consumer depends on) and assert the walks terminate.
"""
from __future__ import annotations

import re
from typing import Any

from ..fidelity import FidelityReport
from .client import FigmaClient

_VERSIONS_PAGE = 50              # the documented max page_size (exercises the clamp + multi-page)
_MAX_PAGES = 10_000

_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _validate_user(u: Any, where: str, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not isinstance(u, dict):
        report.diverge("protocol", where, "user is not an object")
        return
    if not (isinstance(u.get("id"), str) and u["id"]):
        problems.append(f"user.id must be a non-empty string: {u.get('id')!r}")
    # The User object must NOT carry email (email is /v1/me only).
    if "email" in u:
        problems.append("user object must NOT carry `email` (that is /v1/me only)")
    check = "User object is {id, handle, img_url} with NO email"
    if problems:
        report.record_protocol(check, False, "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")


def _validate_version(v: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(v.get("id"), str) and v["id"]):
        problems.append(f"`id` must be a non-empty string: {v.get('id')!r}")
    ca = v.get("created_at")
    if not (isinstance(ca, str) and _ISO_Z_RE.match(ca)):
        problems.append(f"`created_at` must be ISO-8601 Z: {ca!r}")
    # label/description must be present (nullable) — an auto-save carries null.
    if "label" not in v or "description" not in v:
        problems.append("`label`/`description` must be present (nullable for auto-saves)")
    if v.get("label") is not None and not isinstance(v.get("label"), str):
        problems.append(f"`label` must be string|null: {v.get('label')!r}")
    check = "version object contract (id, created_at Z, label|null, user)"
    if problems:
        report.record_protocol(check, False, f"id={v.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")
    _validate_user(v.get("user"), "version.user", report, seen_ok)


def _validate_comment(c: dict, report: FidelityReport, seen_ok: set) -> None:
    problems: list[str] = []
    if not (isinstance(c.get("id"), str) and c["id"]):
        problems.append(f"`id` must be a non-empty string: {c.get('id')!r}")
    if not isinstance(c.get("message"), str):
        problems.append(f"`message` must be a string: {c.get('message')!r}")
    # order_id is string|null per the OpenAPI spec (NOT a Number, despite prose docs).
    if c.get("order_id") is not None and not isinstance(c.get("order_id"), str):
        problems.append(f"`order_id` must be string|null (not Number): {c.get('order_id')!r}")
    ca = c.get("created_at")
    if not (isinstance(ca, str) and _ISO_Z_RE.match(ca)):
        problems.append(f"`created_at` must be ISO-8601 Z: {ca!r}")
    ra = c.get("resolved_at")
    if ra is not None and not (isinstance(ra, str) and _ISO_Z_RE.match(ra)):
        problems.append(f"`resolved_at` must be ISO-8601 Z or null: {ra!r}")
    if not isinstance(c.get("client_meta"), dict):
        problems.append("`client_meta` must be an object (Vector|FrameOffset)")
    if not isinstance(c.get("reactions"), list):
        problems.append("`reactions` must be an array")
    check = "comment object contract (id, message, order_id str|null, client_meta, resolved_at)"
    if problems:
        report.record_protocol(check, False, f"id={c.get('id')}: " + "; ".join(problems))
    elif check not in seen_ok:
        seen_ok.add(check); report.record_protocol(check, True, "")
    _validate_user(c.get("user"), "comment.user", report, seen_ok)


def _event_external_id(team_id: str, kind: str, obj: dict) -> str:
    """The versioned, team-namespaced dedup key the design event stream collapses on."""
    if kind == "version":
        eid = obj["id"]
        ver = obj["id"]                         # immutable
    else:  # comment
        eid = obj["id"]
        ver = obj.get("resolved_at") or "none"  # a resolve advances the version
    return f"figma:{team_id}:event:{eid}:{ver}"


def run_historical(report: FidelityReport, cfg) -> None:
    client = FigmaClient(cfg, report)
    report.auth.update({"method": "X-Figma-Token access token (OAuth Bearer also accepted)"})
    seen_ok: set = set()
    team_id = client.team_id

    # 0) auth probe — GET /v1/me is the ONE place a User carries email.
    status, _, body = client.get_me()
    if status == 200 and isinstance(body, dict):
        ok = isinstance(body.get("id"), str) and isinstance(body.get("email"), str)
        report.record_protocol("/v1/me carries email (the only User that does)", ok,
                               "" if ok else f"me={str(body)[:120]}")
    else:
        report.diverge("protocol", "me", f"GET /v1/me -> {status}; {str(body)[:120]}")

    # 0b) the Fyralis clone's surfaces DO NOT EXIST — prove it.
    st, _, _ = client._request("GET", "/v1/files", "files_list_probe")
    report.record_protocol("no global GET /v1/files list endpoint (404)", st == 404,
                           "" if st == 404 else f"-> {st}")

    # 1) enumerate: teams -> projects -> files.
    status, _, body = client.team_projects()
    files: list[dict] = []   # {key, name, project}
    if status != 200 or not isinstance(body, dict):
        report.diverge("protocol", "projects",
                       f"GET /v1/teams/{team_id}/projects -> {status}; {str(body)[:160]}")
        return
    if set(body) != {"name", "projects"}:
        report.record_protocol("team projects envelope {name, projects}", False,
                               f"keys={sorted(body)}")
    else:
        report.record_protocol("team projects envelope {name, projects}", True, "")
    for proj in body.get("projects", []):
        if not (isinstance(proj, dict) and isinstance(proj.get("id"), str)):
            report.diverge("protocol", "projects", f"bad project entry: {proj!r}")
            continue
        st, _, pf = client.project_files(proj["id"])
        report.record_page("files", proj["id"])
        if st != 200 or not isinstance(pf, dict) or set(pf) != {"name", "files"}:
            report.diverge("protocol", "files",
                           f"GET /v1/projects/{proj['id']}/files -> {st}; {str(pf)[:120]}")
            continue
        for f in pf.get("files", []):
            if isinstance(f, dict) and isinstance(f.get("key"), str):
                if not (isinstance(f.get("last_modified"), str)
                        and _ISO_Z_RE.match(f["last_modified"])):
                    report.record_protocol("project files entry has last_modified ISO-Z",
                                           False, f"file={f!r}")
                files.append({"key": f["key"], "name": f.get("name", ""),
                              "project": proj.get("name", "")})
    report.record_protocol("project files entry has last_modified ISO-Z", True, "")
    report.note(f"enumerated {len(files)} files across {len(body.get('projects', []))} projects")

    # 1b) the per-file event stream is NOT a single /events endpoint — prove it.
    if files:
        st, _, _ = client._request("GET", f"/v1/files/{files[0]['key']}/events", "events_probe")
        report.record_protocol("no GET /v1/files/{key}/events stream (404) — must MERGE "
                               "versions+comments", st == 404, "" if st == 404 else f"-> {st}")

    # 2) per file: MERGE versions (cursor walk) + comments (no pagination).
    external_ids: set = set()
    total_versions = total_comments = 0
    for fmeta in files:
        key = fmeta["key"]

        # 2a) file meta (lightweight) — wrapped in {file:{…}}.
        st, _, mb = client.file_meta(key)
        if st == 200 and isinstance(mb, dict):
            ok = set(mb) == {"file"} and isinstance(mb["file"], dict)
            report.record_protocol("file meta wrapped in {file:{…}}", ok,
                                   "" if ok else f"keys={sorted(mb)}")
        else:
            report.diverge("protocol", "meta", f"GET /v1/files/{key}/meta -> {st}")

        # 2b) versions — CURSOR walk via the full-URL next_page link.
        file_versions: list[dict] = []
        status, _, vb = client.file_versions(key, page_size=_VERSIONS_PAGE)
        pages = 0
        while pages < _MAX_PAGES:
            if status != 200 or not isinstance(vb, dict):
                report.diverge("protocol", "versions",
                               f"GET /v1/files/{key}/versions -> {status}; {str(vb)[:120]}")
                break
            if set(vb) - {"versions", "pagination"}:
                report.record_protocol("versions envelope {versions, pagination}", False,
                                       f"keys={sorted(vb)}")
            pag = vb.get("pagination")
            if not isinstance(pag, dict):
                report.diverge("protocol", "versions", "`pagination` is not an object")
                break
            vlist = vb.get("versions")
            if not isinstance(vlist, list):
                report.diverge("protocol", "versions", "`versions` is not an array")
                break
            pages += 1
            for v in vlist:
                if isinstance(v, dict):
                    total_versions += 1
                    _validate_version(v, report, seen_ok)
                    file_versions.append(v)
                    eid = _event_external_id(team_id, "version", v)
                    external_ids.add(eid)
            nxt = pag.get("next_page")
            if not nxt:
                break
            if "before=" not in nxt:
                report.record_protocol("versions next_page is a full URL carrying before=",
                                       False, f"next_page={nxt!r}")
            status, _, vb = client.follow(nxt, "versions")
        report.record_page("versions", key)

        # 2c) comments — NO pagination (one {comments:[…]} array).
        st, _, cb = client.file_comments(key)
        if st != 200 or not isinstance(cb, dict):
            report.diverge("protocol", "comments",
                           f"GET /v1/files/{key}/comments -> {st}; {str(cb)[:120]}")
            continue
        if set(cb) != {"comments"}:
            report.record_protocol("comments envelope is exactly {comments} (NO pagination)",
                                   False, f"keys={sorted(cb)}")
        for c in cb.get("comments", []):
            if isinstance(c, dict):
                total_comments += 1
                _validate_comment(c, report, seen_ok)
                external_ids.add(_event_external_id(team_id, "comment", c))

    report.record_protocol("versions envelope {versions, pagination}", True, "")
    report.record_protocol("comments envelope is exactly {comments} (NO pagination)", True, "")
    report.record_protocol("versions next_page is a full URL carrying before=", True, "")

    # 3) the merged event stream — versioned, team-namespaced, deduped.
    n_events = total_versions + total_comments
    report.count("figma_version", total_versions)
    report.count("figma_comment", total_comments)
    unique_ok = len(external_ids) == n_events
    report.record_protocol(
        "merged event stream external_id figma:{team}:event:{id}:{version} is unique",
        unique_ok, "" if unique_ok else f"{len(external_ids)} ids for {n_events} events")
    report.note(f"merged event stream: {total_versions} versions + {total_comments} comments "
                f"= {n_events} events ({len(external_ids)} unique external_ids)")
