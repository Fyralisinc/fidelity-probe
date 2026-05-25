"""Notion historical ingestion — search/enumerate → paginate → fetch full objects.

  * POST /v1/search           enumerate every page & database the integration can see,
                              paginated by start_cursor / next_cursor.
  * GET  /v1/pages/{id}       fetch each page in full; GET /v1/blocks/{id}/children for
                              its block content (paginated).
  * GET  /v1/databases/{id}   fetch each database; POST /v1/databases/{id}/query for rows.
  * GET  /v1/users            enumerate workspace users (paginated).

Every list envelope is validated against the documented pagination contract and each
object against its documented shape (specs/notion.openapi.json). Full-object fetches
are capped per run to stay bounded.
"""
from __future__ import annotations

from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from .client import NotionClient

_PAGE = 100
_FULL_CAP = 25  # full page/database fetches per run


def _check_list(sv: SpecValidator, body, path: str, report: FidelityReport, label: str) -> None:
    sv.validate_response(body, path, report, method="post" if path == "/v1/search"
                         or path.endswith("/query") else "get", label=label)


def search_all(client: NotionClient, sv: SpecValidator, report: FidelityReport) -> list[dict]:
    results: list[dict] = []
    cursor: str | None = None
    while True:
        payload: dict = {"page_size": _PAGE}
        if cursor:
            payload["start_cursor"] = cursor
        status, _, body = client.post("/v1/search", "search", payload)
        report.record_page("search", cursor)
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "search", f"POST /v1/search -> {status}; {str(body)[:160]}")
            break
        sv.validate_response(body, "/v1/search", report, method="post", label="search")
        for obj in body.get("results", []):
            results.append(obj)
            kind = obj.get("object")
            comp = {"page": "Page", "database": "Database"}.get(kind)
            if comp:
                sv.validate_against_component(obj, comp, report)
            report.count(f"search:{kind}")
        cursor = body.get("next_cursor")
        if not body.get("has_more") or not cursor:
            break
    return results


def fetch_block_children(client: NotionClient, sv: SpecValidator, block_id: str,
                         report: FidelityReport) -> None:
    cursor: str | None = None
    while True:
        params = {"page_size": _PAGE}
        if cursor:
            params["start_cursor"] = cursor
        status, _, body = client.get(f"/v1/blocks/{block_id}/children", "blocks.children", params)
        report.record_page("blocks.children", cursor)
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "blocks.children",
                           f"GET /v1/blocks/{block_id}/children -> {status}")
            break
        sv.validate_response(body, "/v1/blocks/{id}/children", report, label="blocks.children")
        for blk in body.get("results", []):
            sv.validate_against_component(blk, "Block", report)
            report.count("block")
        cursor = body.get("next_cursor")
        if not body.get("has_more") or not cursor:
            break


def list_users(client: NotionClient, sv: SpecValidator, report: FidelityReport) -> None:
    cursor: str | None = None
    total = 0
    while True:
        params = {"page_size": _PAGE}
        if cursor:
            params["start_cursor"] = cursor
        status, _, body = client.get("/v1/users", "users", params)
        report.record_page("users", cursor)
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "users", f"GET /v1/users -> {status}")
            break
        sv.validate_response(body, "/v1/users", report, label="users")
        for u in body.get("results", []):
            sv.validate_against_component(u, "User", report)
            total += 1
        cursor = body.get("next_cursor")
        if not body.get("has_more") or not cursor:
            break
    report.count("user", total)


def run_historical(report: FidelityReport, cfg, full_cap: int = _FULL_CAP) -> None:
    sv = SpecValidator("notion")
    client = NotionClient(cfg, report)
    report.auth.update({"method": "internal integration (Bearer token)",
                        "notion_version": cfg.version})

    found = search_all(client, sv, report)
    pages = [o for o in found if o.get("object") == "page"]
    databases = [o for o in found if o.get("object") == "database"]
    report.count("page", len(pages))
    report.count("database", len(databases))

    # Fetch full objects (bounded): pages + their block children.
    for pg in pages[:full_cap]:
        status, _, body = client.get(f"/v1/pages/{pg['id']}", "pages.get")
        if status == 200:
            sv.validate_against_component(body, "Page", report)
            fetch_block_children(client, sv, pg["id"], report)
        else:
            report.diverge("protocol", "pages.get", f"GET /v1/pages/{pg['id']} -> {status}")

    # Fetch full databases + a page of their rows.
    for db in databases[:full_cap]:
        status, _, body = client.get(f"/v1/databases/{db['id']}", "databases.get")
        if status == 200:
            sv.validate_against_component(body, "Database", report)
        status, _, q = client.post(f"/v1/databases/{db['id']}/query", "databases.query",
                                    {"page_size": _PAGE})
        report.record_page("databases.query", None)
        if status == 200 and isinstance(q, dict):
            sv.validate_response(q, "/v1/databases/{id}/query", report,
                                 method="post", label="databases.query")
            for row in q.get("results", []):
                report.count("db_row")

    list_users(client, sv, report)
