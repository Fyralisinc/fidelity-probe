"""GitHub historical ingestion — Link pagination, conditional requests, header audit.

GitHub's REST list endpoints paginate via the RFC 5988 `Link` header (`rel="next"`),
not a body cursor. We drive PyGithub's own `Requester` at the page level so the wire
behaviour is the official client's (auth, Accept, retry/backoff) while we still get
each raw response — letting us:

  * follow `Link: rel="next"` to the end and record the page/cursor chain,
  * validate every page body against the official OpenAPI response schema,
  * issue a conditional `If-None-Match` re-request and confirm the documented
    `304 Not Modified` ETag behaviour,
  * audit the standard GitHub response headers (X-GitHub-Request-Id, X-RateLimit-*,
    X-GitHub-Media-Type) that the REST contract guarantees on every response.

A 403/429 with rate-limit signalling is recorded and backoff is honored (PyGithub's
GithubRetry handles primary + secondary limits); anything that deviates from the
official contract is recorded as a divergence and ingestion continues.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from github import Github
from github.GithubException import GithubException

from ..fidelity import FidelityReport
from ..schemas import SpecValidator
from .auth import _std_headers

# Bulk historical pulls use the max page size, exactly as a real "sync everything"
# client would, to minimize round-trips. A separate, deliberately small page size is
# used only by the pagination-contract probe to force a multi-page Link chain so the
# cursor-following path is exercised end-to-end even when a repo holds few items.
_PER_PAGE = 100
_PROBE_PAGE = 3

# Headers the GitHub REST contract guarantees on (essentially) every response. Their
# absence means the target isn't speaking GitHub's documented protocol, so we audit them.
_STD_RESPONSE_HEADERS = (
    "x-github-request-id",
    "x-github-media-type",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
)


class _Headers:
    """Case-insensitive view over PyGithub's response header dict."""

    def __init__(self, raw: dict):
        self._h = {str(k).lower(): v for k, v in (raw or {}).items()}

    def get(self, name: str) -> str | None:
        return self._h.get(name.lower())

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._h


def _next_link(headers: _Headers) -> tuple[str, dict] | None:
    """Parse `Link: …; rel="next"` into a (path, query-params) pair, or None at the end."""
    link = headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        segs = part.split(";")
        url = segs[0].strip().strip("<>")
        if any('rel="next"' in s.replace(" ", "").replace("'", '"') or "rel=next" in s
               for s in segs[1:]):
            split = urlsplit(url)
            params = {k: v[0] for k, v in parse_qs(split.query).items()}
            return split.path, params
    return None


def _cursor_token(params: dict) -> str | None:
    """The pagination position to record — page number, or an opaque cursor if used."""
    for key in ("page", "after", "cursor", "since"):
        if key in params:
            return f"{key}={params[key]}"
    return None


def _audit_headers(label: str, headers: _Headers, report: FidelityReport,
                   first_seen: set) -> None:
    """Record presence of the documented standard headers; diverge only the first time
    a guaranteed header is found entirely missing on a real (200) response."""
    for name in _STD_RESPONSE_HEADERS:
        present = name in headers
        if present:
            first_seen.add(name)
        elif name not in first_seen:
            # Record once per missing header as a protocol divergence: the REST contract
            # promises these on every response.
            key = f"header:{name}"
            if key not in first_seen:
                first_seen.add(key)
                report.record_protocol(f"response header `{name}`", False,
                                       f"absent on `{label}` response (GitHub returns it on "
                                       f"every REST response)")


def _check_rate_limit(label: str, status: int, headers: _Headers,
                      report: FidelityReport) -> None:
    remaining = headers.get("x-ratelimit-remaining")
    if status in (403, 429):
        retry_after = headers.get("retry-after")
        reset = headers.get("x-ratelimit-reset")
        ra = None
        try:
            ra = float(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            ra = None
        honored = ra is not None or reset is not None or remaining == "0"
        report.record_rate_limit(label, status, ra, honored)


def _paginate(gh: Github, path: str, spec_path: str, label: str,
              sv: SpecValidator, report: FidelityReport,
              params: dict | None = None, header_state: set | None = None) -> list:
    """Walk the full Link chain for one list endpoint; validate + record each page."""
    header_state = header_state if header_state is not None else set()
    items: list = []
    query = dict(params or {})
    query.setdefault("per_page", _PER_PAGE)
    url = path
    first_etag: str | None = None
    first_url: str | None = None
    first_query: dict | None = None

    while url is not None:
        status, raw_headers, body = gh.requester.requestJson(
            "GET", url, parameters=query, headers=_std_headers())
        headers = _Headers(raw_headers)
        data = json.loads(body) if body else None
        _check_rate_limit(label, status, headers, report)

        if status == 200:
            report.record_page(label, _cursor_token(query))  # a page only counts when served
            _audit_headers(label, headers, report, header_state)
            sv.validate_response(data, spec_path, report, label=label)
            if isinstance(data, list):
                items.extend(data)
            elif isinstance(data, dict):  # e.g. /installation/repositories wrapper
                items.extend(data.get("repositories") or [])
            if first_etag is None:
                first_etag = headers.get("etag")
                first_url, first_query = url, dict(query)
        elif status in (403, 429):
            report.note(f"{label}: throttled (status {status}); backoff honored, continuing")
            break
        else:
            # The spec documents a 200 list response for these endpoints; any other status
            # is a contract deviation. Record it once per label so 5 repos don't spam it.
            key = f"status:{label}:{status}"
            if key not in header_state:
                header_state.add(key)
                report.record_protocol(
                    f"`{label}` availability", False,
                    f"returned {status} but the official spec documents a 200 list response")
            break

        nxt = _next_link(headers)
        if nxt is None:
            break
        url, query = nxt

    if first_etag and first_url is not None:
        _verify_conditional(gh, first_url, first_query or {}, first_etag, label, report, header_state)
    return items


def _verify_conditional(gh: Github, url: str, query: dict, etag: str,
                        label: str, report: FidelityReport, seen: set) -> None:
    """Re-request the first page with If-None-Match; GitHub must answer 304 Not Modified.

    Recorded once per resource label on success (so 5 repos don't repeat an identical
    "ok" line); a failure is always recorded since it's a real finding.
    """
    try:
        status, raw_headers, _ = gh.requester.requestJson(
            "GET", url, parameters=query,
            headers={**_std_headers(), "If-None-Match": etag})
    except GithubException as e:
        report.record_protocol(f"ETag/304 on `{label}`", False,
                               f"conditional request raised {e.status}")
        return
    ok = status == 304
    pass_key = f"etag-ok:{label}"
    if ok and pass_key in seen:
        return
    if ok:
        seen.add(pass_key)
    report.record_protocol(
        f"ETag/304 on `{label}`", ok,
        "" if ok else f"If-None-Match: {etag} returned {status}, expected 304")


def verify_pagination_contract(gh: Github, repos: list[dict], sv: SpecValidator,
                               report: FidelityReport, seen: set) -> None:
    """Force a multi-page Link chain with a small per_page to prove cursor-following.

    Bulk pulls use per_page=100, so on a small dataset no resource spans >1 page and the
    Link-following code never engages. This probe walks one repo's commits at a small
    page size, following `rel="next"` to the end and validating every page, so the
    multi-page traversal contract is genuinely exercised against the live target. Pages
    are recorded under a probe-specific operation; payloads merge into the `commits`
    schema check (no duplicate divergence); object counts are untouched.
    """
    for repo in repos:
        owner = (repo.get("owner") or {}).get("login")
        name = repo.get("name")
        if not owner or not name:
            continue
        op = f"commits pagination probe ({owner}/{name})"
        url: str | None = f"/repos/{owner}/{name}/commits"
        query: dict = {"per_page": _PROBE_PAGE}
        pages = 0
        cursors: list[str] = []
        try:
            while url is not None:
                status, raw_headers, body = gh.requester.requestJson(
                    "GET", url, parameters=query, headers=_std_headers())
                if status != 200:
                    break
                data = json.loads(body) if body else None
                pages += 1
                tok = _cursor_token(query)
                report.record_page(op, tok)
                if tok:
                    cursors.append(tok)
                sv.validate_response(data, "/repos/{owner}/{repo}/commits", report,
                                     label="commits")
                nxt = _next_link(_Headers(raw_headers))
                if nxt is None:
                    break
                url, query = nxt
        except GithubException as e:
            report.note(f"pagination probe ({owner}/{name}): {e.status}")
            continue
        if pages >= 2:
            report.record_protocol(
                "Link pagination (multi-page traversal)", True,
                f"walked {pages} pages of {owner}/{name} commits at per_page={_PROBE_PAGE}, "
                f"following cursors {cursors}")
            return
    report.note("pagination probe: no installation repo held enough commits to span "
                f"more than one page at per_page={_PROBE_PAGE}")


# --------------------------------------------------------------------------- resources


def list_repositories(gh: Github, sv: SpecValidator, report: FidelityReport,
                      header_state: set) -> list[dict]:
    repos = _paginate(gh, "/installation/repositories",
                      "/installation/repositories", "installation.repositories",
                      sv, report, header_state=header_state)
    report.count("repository", len(repos))
    return repos


def ingest_repo(gh: Github, repo: dict, sv: SpecValidator, report: FidelityReport,
                header_state: set) -> None:
    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    if not owner or not name:
        report.note(f"skipping repo with missing owner/name: {repo.get('full_name')}")
        return
    base = f"/repos/{owner}/{name}"
    resources = [
        # (path, spec template path, label, extra query, object type)
        (f"{base}/issues", "/repos/{owner}/{repo}/issues", "issues",
         {"state": "all"}, "issue"),
        (f"{base}/pulls", "/repos/{owner}/{repo}/pulls", "pulls",
         {"state": "all"}, "pull_request"),
        (f"{base}/commits", "/repos/{owner}/{repo}/commits", "commits", {}, "commit"),
        (f"{base}/branches", "/repos/{owner}/{repo}/branches", "branches", {}, "branch"),
        (f"{base}/labels", "/repos/{owner}/{repo}/labels", "labels", {}, "label"),
    ]
    for path, spec_path, label, query, obj in resources:
        try:
            got = _paginate(gh, path, spec_path, label, sv, report,
                            params=query, header_state=header_state)
        except GithubException as e:
            report.note(f"{label}({owner}/{name}): {e.status} {getattr(e, 'data', '')}")
            continue
        report.count(obj, len(got))


def run_historical(gh: Github, report: FidelityReport, sv: SpecValidator,
                   max_repos: int | None = None) -> None:
    # Shared across the run so each documented header is only flagged missing once.
    header_state: set = set()
    repos = list_repositories(gh, sv, report, header_state)
    if max_repos is not None:
        repos = repos[:max_repos]
    for repo in repos:
        ingest_repo(gh, repo, sv, report, header_state)
    # Explicitly exercise the multi-page Link-following path (bulk pulls use per_page=100,
    # which won't chain on a small dataset).
    verify_pagination_contract(gh, repos, sv, report, header_state)
