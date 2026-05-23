"""GitHub App auth — the real two-legged flow, done by hand.

GitHub Apps authenticate in two legs (https://docs.github.com/apps/...):

  1. App JWT: sign a short-lived RS256 JWT with the App's PEM private key. The
     payload is `{iss: <app id>, iat, exp}` (exp <= 10 min out); GitHub clock-skews
     iat back 60s. This proves "I am the App".
  2. Installation token: POST /app/installations/{installation_id}/access_tokens
     with `Authorization: Bearer <jwt>`. GitHub returns a `ghs_…` installation token
     (1 hour TTL) scoped to that installation. All subsequent REST reads use it as
     `Authorization: token ghs_…`.

We mint the JWT ourselves with PyJWT (RS256) — exactly what PyGithub/octokit do
internally — rather than leaning on a convenience wrapper, because the task is to
exercise the real flow. PyGithub still carries the request (its Requester adds the
Authorization/Accept/User-Agent headers and applies retry/backoff), so the wire
shape is the official one. We then validate the token response against the official
spec and hand the `ghs_` token to a normal authenticated `Github` client.

The only configured knob is the base URL.
"""
from __future__ import annotations

import time

import jwt
from github import Auth, Github
from github.GithubException import GithubException

from ..config import GitHubConfig
from ..fidelity import FidelityReport
from ..schemas import SpecValidator

# GitHub's standard request headers. Accept + Authorization + User-Agent are added by
# PyGithub's Requester; the API-version pin is a documented header it does not set, so
# we add it explicitly — it is part of the official request shape, not a mock knob.
API_VERSION = "2022-11-28"
ACCEPT = "application/vnd.github+json"

# JWT lifetime. iat backdated 60s to tolerate clock skew; exp well under GitHub's 10-min cap.
_JWT_BACKDATE = 60
_JWT_TTL = 540


def mint_app_jwt(app_id: str, private_key: str, *, now: int | None = None) -> str:
    """Sign the App JWT (RS256, iss = app id). This is leg 1 of GitHub App auth."""
    now = int(time.time()) if now is None else now
    payload = {"iat": now - _JWT_BACKDATE, "exp": now + _JWT_TTL, "iss": str(app_id)}
    token = jwt.encode(payload, private_key, algorithm="RS256")
    # PyJWT<2 returned bytes; normalize.
    return token.decode() if isinstance(token, bytes) else token


def _std_headers() -> dict[str, str]:
    return {"Accept": ACCEPT, "X-GitHub-Api-Version": API_VERSION}


def fetch_app_identity(app_jwt: str, cfg: GitHubConfig, sv: SpecValidator,
                       report: FidelityReport) -> dict | None:
    """GET /app as the App (JWT auth) to confirm identity; validate vs `integration`."""
    gh = Github(auth=Auth.AppAuthToken(app_jwt), base_url=cfg.base_url)
    try:
        headers, data = gh.requester.requestJsonAndCheck("GET", "/app", headers=_std_headers())
    except GithubException as e:
        report.note(f"GET /app failed under app JWT: {e.status} {e.data}")
        return None
    sv.validate_response(data, "/app", report, label="app")
    if isinstance(data, dict):
        report.auth.update({
            "app": data.get("name") or data.get("slug"),
            "app_id_reported": data.get("id"),
        })
    return data


def request_installation_token(app_jwt: str, cfg: GitHubConfig, sv: SpecValidator,
                               report: FidelityReport) -> str:
    """Leg 2: exchange the App JWT for a ghs_ installation token; validate the response."""
    _, _, installation_id = cfg.require_app_auth()
    gh = Github(auth=Auth.AppAuthToken(app_jwt), base_url=cfg.base_url)
    path = f"/app/installations/{installation_id}/access_tokens"
    # POST with no body grants the App's full configured permission set, like the docs' default.
    headers, data = gh.requester.requestJsonAndCheck("POST", path, headers=_std_headers())
    # The spec templates the installation_id, so validate against the templated path.
    sv.validate_response(
        data, "/app/installations/{installation_id}/access_tokens", report,
        method="post", status="201", label="access_tokens",
    )
    token = data.get("token") if isinstance(data, dict) else None
    if not token:
        raise RuntimeError("access_tokens response contained no `token`")
    if not token.startswith("ghs_"):
        report.diverge("protocol", "access_tokens",
                       f"installation token does not use the documented `ghs_` prefix "
                       f"(got `{token[:4]}…`)")
    report.auth.update({
        "method": "GitHub App JWT (RS256) → installation access token",
        "installation_id": installation_id,
        "token_prefix": token[:4],
        "token_expires_at": data.get("expires_at"),
    })
    return token


def make_installation_client(token: str, cfg: GitHubConfig) -> Github:
    """Authenticated client for installation-scoped REST reads (Authorization: token …)."""
    return Github(auth=Auth.Token(token), base_url=cfg.base_url, per_page=100)


def acquire_client(cfg: GitHubConfig, sv: SpecValidator,
                   report: FidelityReport) -> Github:
    """Run both auth legs and return a ready-to-read installation client."""
    app_id, private_key, _ = cfg.require_app_auth()
    app_jwt = mint_app_jwt(app_id, private_key)
    fetch_app_identity(app_jwt, cfg, sv, report)
    token = request_installation_token(app_jwt, cfg, sv, report)
    return make_installation_client(token, cfg)
