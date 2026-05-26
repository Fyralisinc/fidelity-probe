"""Jira historical ingestion — enumerate projects → page issues with JQL → changelog.

  * GET /rest/api/3/project/search   enumerate projects (classic startAt/maxResults/total/
                                     isLast/values pagination).
  * GET /rest/api/3/search/jql       per project, `project = "KEY" ORDER BY updated ASC`,
                                     expand=changelog, paged with the NEW token model
                                     (nextPageToken / isLast — no startAt/total). The old
                                     /rest/api/3/search was removed in 2025.

Each issue → jira:issue; STATUS changelog transitions are counted as state_changes. The
slice asserts the new token pagination actually terminates (a known real-world failure
mode), records the incremental cursor (max updated), and validates every response
against the documented contracts (specs/jira.openapi.json).
"""
from __future__ import annotations

from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from .client import JiraClient

_PROJECT_PAGE = 50
_ISSUE_PAGE = 100
_MAX_ISSUE_PAGES = 1000  # safety bound against non-terminating pagination
_ISSUE_FIELDS = "summary,status,updated,created,issuetype,assignee,reporter"


def enumerate_projects(client: JiraClient, sv: SpecValidator,
                       report: FidelityReport) -> list[dict]:
    projects: list[dict] = []
    start = 0
    while True:
        status, _, body = client.get("/rest/api/3/project/search", "project.search",
                                     {"startAt": start, "maxResults": _PROJECT_PAGE})
        report.record_page("project.search", str(start))
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "project.search",
                           f"GET /rest/api/3/project/search -> {status}; {str(body)[:160]}")
            break
        sv.validate_response(body, "/rest/api/3/project/search", report, label="project.search")
        values = body.get("values") or []
        projects.extend(values)
        if body.get("isLast") or not values:
            break
        start += len(values)
    report.count("project", len(projects))
    return projects


def _count_status_transitions(issue: dict, report: FidelityReport) -> None:
    changelog = issue.get("changelog") or {}
    for hist in changelog.get("histories", []):
        for item in hist.get("items", []):
            if item.get("field") == "status":
                report.count("status_transition")


def probe_jql_get(client: JiraClient, report: FidelityReport) -> None:
    """The real /search/jql supports BOTH GET (small queries) and POST. We backfill with
    POST (robust, body JQL), but probe GET once and report if the target rejects the
    documented GET method — and whether its error envelope matches Jira's."""
    status, _, body = client.get("/rest/api/3/search/jql", "search.jql.get_probe",
                                 {"jql": "ORDER BY created DESC", "maxResults": 1})
    ok = status == 200
    detail = ""
    if not ok:
        env_ok = isinstance(body, dict) and ("errorMessages" in body or "errors" in body)
        detail = (f"GET returned {status} (real Jira supports GET for small JQL queries)"
                  + ("" if env_ok else f"; error body is not Jira's envelope "
                     f"{{errorMessages,errors}}: {str(body)[:80]}"))
    report.record_protocol("Jira /search/jql GET method (documented)", ok, detail)


def page_issues(client: JiraClient, sv: SpecValidator, report: FidelityReport,
                project_key: str) -> str | None:
    """Page a project's issues via the new token-paginated /search/jql (POST). Returns the
    max `updated` seen (the incremental cursor) and asserts pagination terminates."""
    jql = f'project = "{project_key}" ORDER BY updated ASC'
    next_token: str | None = None
    seen_tokens: set[str] = set()
    pages = 0
    max_updated: str | None = None
    terminated = False
    while pages < _MAX_ISSUE_PAGES:
        payload = {"jql": jql, "maxResults": _ISSUE_PAGE,
                   "fields": _ISSUE_FIELDS.split(","), "expand": "changelog"}
        if next_token:
            payload["nextPageToken"] = next_token
        status, _, body = client.post("/rest/api/3/search/jql", "search.jql", payload)
        report.record_page("search.jql", next_token)
        if status != 200 or not isinstance(body, dict):
            report.diverge("protocol", "search.jql",
                           f"GET /rest/api/3/search/jql -> {status}; {str(body)[:160]}")
            return max_updated
        sv.validate_response(body, "/rest/api/3/search/jql", report, label="search.jql")
        for issue in body.get("issues") or []:
            report.count("issue")
            _count_status_transitions(issue, report)
            upd = (issue.get("fields") or {}).get("updated")
            if upd and (max_updated is None or upd > max_updated):
                max_updated = upd
        pages += 1
        is_last = body.get("isLast")
        next_token = body.get("nextPageToken")
        if is_last or not next_token:
            terminated = True
            break
        if next_token in seen_tokens:
            report.record_protocol(
                f"Jira /search/jql pagination terminates ({project_key})", False,
                "nextPageToken repeated — pagination is not advancing (would loop forever)")
            return max_updated
        seen_tokens.add(next_token)
    report.record_protocol(
        f"Jira /search/jql pagination terminates ({project_key})", terminated,
        "" if terminated else f"hit the {_MAX_ISSUE_PAGES}-page safety cap without isLast")
    return max_updated


def run_historical(report: FidelityReport, cfg, max_projects: int | None = None) -> None:
    sv = SpecValidator("jira")
    client = JiraClient(cfg, report)
    report.auth.update({"method": "HTTP Basic (account_email:api_token)",
                        "site": cfg.base_url})
    projects = enumerate_projects(client, sv, report)
    probe_jql_get(client, report)
    keys = [p["key"] for p in projects if p.get("key")]
    if max_projects is not None:
        keys = keys[:max_projects]
    cursors: dict[str, str] = {}
    for key in keys:
        cursor = page_issues(client, sv, report, key)
        if cursor:
            cursors[key] = cursor
    if cursors:
        report.note(f"incremental cursors (max updated per project): {cursors}")
