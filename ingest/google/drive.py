"""Google Drive historical ingestion + incremental changes feed (Drive API v3).

Per the integration model (same SA + domain-wide-delegation auth as Gmail/Calendar,
scope drive.readonly):
  * enumerate targets: each user's My Drive (impersonated via the Directory) plus the
    org's Shared Drives (drives.list),
  * backfill files (files.list — for shared drives with the
    includeItemsFromAllDrives/supportsAllDrives/driveId/corpora params the real API
    requires), and per file its comments (comments.list — which *requires* a `fields`
    param) and revisions (revisions.list),
  * export Google-native docs (files.export?mimeType=…, which returns raw bytes, not
    JSON) to exercise text extraction, with a byte cap, and
  * incremental: capture changes.getStartPageToken at backfill start, then exercise
    changes.list?pageToken=… and confirm the feed terminates with a newStartPageToken.

Responses are validated against the official Drive v3 discovery schemas (FileList, File,
CommentList, RevisionList, DriveList, ChangeList, StartPageToken). 429/Retry-After is
honored by the shared transport.
"""
from __future__ import annotations

import json

import requests

from ..config import DRIVE_SCOPE, GoogleConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from . import auth, directory, transport

_FILE_PAGE = 100
_FILE_CAP = 50          # full (comments/revisions/export) handling per target
_EXPORT_BYTE_CAP = 1_000_000

# Google-native MIME types and the export MIME we request for each.
_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
_FILE_FIELDS = ("nextPageToken,incompleteSearch,"
                "files(id,name,mimeType,trashed,modifiedTime,parents,driveId,size)")
_COMMENT_FIELDS = "nextPageToken,comments(id,content,author,createdTime,resolved)"
_REVISION_FIELDS = "nextPageToken,revisions(id,modifiedTime,lastModifyingUser,size)"
_CHANGE_FIELDS = ("nextPageToken,newStartPageToken,"
                  "changes(fileId,removed,time,changeType,driveId,file(id,name,mimeType,trashed))")


def _list_shared_drives(cfg, token, sv, report, session) -> list[dict]:
    drives: list[dict] = []
    page_token = None
    url = f"{cfg.drive_base}/drives"
    while True:
        params = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        status, _, body = transport.get(session, url, token, "drive.drives.list", report, params)
        report.record_page("drive.drives.list", page_token)
        if status != 200:
            # drives.list is admin/shared-drive scoped; a non-200 here isn't fatal.
            report.note(f"drives.list -> {status}: {str(body)[:140]}")
            break
        sv.validate_against_component(body, "DriveList", report)
        drives.extend(body.get("drives") or [])
        page_token = body.get("nextPageToken") if isinstance(body, dict) else None
        if not page_token:
            break
    report.count("shared_drive", len(drives))
    return drives


def _list_files(cfg, token, sv, report, session, *, drive_id: str | None) -> list[dict]:
    files: list[dict] = []
    page_token = None
    url = f"{cfg.drive_base}/files"
    op = "drive.files.list"
    while True:
        params = {"pageSize": _FILE_PAGE, "fields": _FILE_FIELDS}
        if drive_id:  # a specific shared drive
            params.update({"corpora": "drive", "driveId": drive_id,
                           "includeItemsFromAllDrives": "true", "supportsAllDrives": "true"})
        if page_token:
            params["pageToken"] = page_token
        status, _, body = transport.get(session, url, token, op, report, params)
        report.record_page(op, page_token)
        if status != 200:
            report.diverge("protocol", op, f"GET {url} -> {status}; body={str(body)[:160]}")
            break
        sv.validate_against_component(body, "FileList", report)
        for f in (body.get("files") or []):
            files.append(f)
            report.count("file")
            if f.get("trashed"):
                report.count("file_trashed")
        page_token = body.get("nextPageToken") if isinstance(body, dict) else None
        if not page_token:
            break
    return files


def _file_comments(cfg, token, sv, report, session, file_id: str) -> None:
    # comments.list REQUIRES the `fields` query param; omitting it is a 400 on real Drive.
    status, _, body = transport.get(session, f"{cfg.drive_base}/files/{file_id}/comments",
                                    token, "drive.comments.list", report,
                                    {"fields": _COMMENT_FIELDS, "pageSize": 100})
    report.record_page("drive.comments.list", None)
    if status == 200:
        sv.validate_against_component(body, "CommentList", report)
        report.count("comment", len(body.get("comments") or []))
    elif status != 404:
        report.diverge("protocol", "drive.comments.list",
                       f"comments({file_id}) -> {status}; body={str(body)[:140]}")


def _file_revisions(cfg, token, sv, report, session, file_id: str) -> None:
    status, _, body = transport.get(session, f"{cfg.drive_base}/files/{file_id}/revisions",
                                    token, "drive.revisions.list", report,
                                    {"fields": _REVISION_FIELDS, "pageSize": 100})
    report.record_page("drive.revisions.list", None)
    if status == 200:
        sv.validate_against_component(body, "RevisionList", report)
        report.count("revision", len(body.get("revisions") or []))
    elif status != 404:
        report.note(f"revisions({file_id}) -> {status}")


def _export(cfg, token, report, session, file_id: str, mime: str) -> None:
    export_mime = _EXPORT_MIME[mime]
    status, headers, body = transport.get(
        session, f"{cfg.drive_base}/files/{file_id}/export", token, "drive.files.export",
        report, {"mimeType": export_mime})
    if status != 200:
        report.diverge("protocol", "drive.files.export",
                       f"export({file_id}, {export_mime}) -> {status}; body={str(body)[:140]}")
        return
    # export returns raw bytes, not JSON — record extracted size (capped), don't parse.
    text = body if isinstance(body, str) else json.dumps(body)
    report.count("exported_text_bytes", min(len(text.encode("utf-8")), _EXPORT_BYTE_CAP))
    report.count("file_exported")


def _verify_changes_feed(cfg, token, sv, report, session) -> None:
    """getStartPageToken → changes.list; confirm the feed yields a newStartPageToken."""
    status, _, body = transport.get(session, f"{cfg.drive_base}/changes/startPageToken",
                                    token, "drive.changes.getStartPageToken", report,
                                    {"supportsAllDrives": "true"})
    if status != 200 or not isinstance(body, dict):
        report.record_protocol("Drive changes.getStartPageToken", False,
                               f"-> {status}; body={str(body)[:140]}")
        return
    sv.validate_against_component(body, "StartPageToken", report)
    start = body.get("startPageToken")
    report.record_protocol("Drive changes.getStartPageToken returns startPageToken",
                           bool(start), "" if start else "no startPageToken in response")
    if not start:
        return
    page_token = start
    saw_new_start = False
    for _ in range(20):  # bounded walk of the changes feed
        params = {"pageToken": page_token, "pageSize": 100, "fields": _CHANGE_FIELDS,
                  "includeItemsFromAllDrives": "true", "supportsAllDrives": "true"}
        status, _, body = transport.get(session, f"{cfg.drive_base}/changes", token,
                                        "drive.changes.list", report, params)
        report.record_page("drive.changes.list", page_token)
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "drive.changes.list", f"-> {status}; {str(body)[:140]}")
            return
        sv.validate_against_component(body, "ChangeList", report)
        report.count("change", len(body.get("changes") or []))
        nxt = body.get("nextPageToken")
        if body.get("newStartPageToken"):
            saw_new_start = True
        if nxt:
            page_token = nxt
        else:
            break
    report.record_protocol(
        "Drive changes.list terminates with newStartPageToken", saw_new_start,
        "" if saw_new_start else "feed ended without a newStartPageToken (incremental sync "
        "would have no resume point)")


def _ingest_target(cfg, token, sv, report, session, *, label: str,
                   drive_id: str | None, file_cap: int) -> None:
    files = _list_files(cfg, token, sv, report, session, drive_id=drive_id)
    for f in files[:file_cap]:
        fid = f.get("id")
        if not fid:
            continue
        _file_comments(cfg, token, sv, report, session, fid)
        _file_revisions(cfg, token, sv, report, session, fid)
        mime = f.get("mimeType")
        if mime in _EXPORT_MIME:
            _export(cfg, token, report, session, fid, mime)


def run_historical(cfg: GoogleConfig, report: FidelityReport,
                   max_users: int | None = None, file_cap: int = _FILE_CAP) -> None:
    sv_dir = SpecValidator("admin_directory")
    sv = SpecValidator("drive")
    key_pem = auth.resolve_key(cfg, report)
    session = requests.Session()

    users = directory.list_users(cfg, key_pem, sv_dir, report, session)
    emails = [u["primaryEmail"] for u in users if u.get("primaryEmail")]
    if max_users is not None:
        emails = emails[:max_users]

    # Shared Drives are visible to a user token; enumerate via the first user.
    if emails:
        first_token = auth.fetch_token(cfg, key_pem, cfg.drive_token_url, emails[0],
                                       DRIVE_SCOPE, report, session=session)
        shared = _list_shared_drives(cfg, first_token, sv, report, session)
        for d in shared:
            _ingest_target(cfg, first_token, sv, report, session,
                           label=f"shared:{d.get('id')}", drive_id=d.get("id"), file_cap=file_cap)
        _verify_changes_feed(cfg, first_token, sv, report, session)

    # Each user's My Drive (impersonated).
    for email in emails:
        token = auth.fetch_token(cfg, key_pem, cfg.drive_token_url, email, DRIVE_SCOPE,
                                 report, session=session)
        _ingest_target(cfg, token, sv, report, session, label=f"mydrive:{email}",
                       drive_id=None, file_cap=file_cap)
    report.count("drive_target", len(emails))
