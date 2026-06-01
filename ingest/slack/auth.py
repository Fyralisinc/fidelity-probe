"""Slack auth — the real OAuth v2 flow, plus a production-shaped WebClient builder.

OAuth v2 (authorization code grant):
  1. Build the authorize URL and send the user to the consent screen.
  2. Slack redirects back to redirect_uri with ?code=...
  3. Exchange the code at oauth.v2.access for a bot token.

We support three ways to obtain the code, in order of preference:
  - SLACK_BOT_TOKEN set        -> use it directly (service handed us a token).
  - SLACK_OAUTH_CODE set       -> exchange that code (non-interactive).
  - otherwise                  -> run a one-shot local callback server, print the
                                  authorize URL, and capture the redirect.

The WebClient is built exactly as in production: base_url from env, real
rate-limit + connection retry handlers attached. The only addition is a recording
handler that *observes* (does not alter) retries so the fidelity report can show
that 429 backoff was exercised and honored.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import (
    ConnectionErrorRetryHandler,
    RateLimitErrorRetryHandler,
)
from slack_sdk.http_retry.request import HttpRequest
from slack_sdk.http_retry.response import HttpResponse
from slack_sdk.http_retry.state import RetryState
from slack_sdk.oauth import AuthorizeUrlGenerator

from ..config import SlackConfig
from ..fidelity import FidelityReport


@dataclass(frozen=True)
class TokenSet:
    """The two tokens an OAuth v2 install yields.

    ``bot`` (xoxb) reads channels; ``user`` (xoxp) reads the consenting human's
    DMs. ``user`` is None when no user scopes were granted (a bot-only install).
    """
    bot: str
    user: str | None


class _RecordingRateLimitHandler(RateLimitErrorRetryHandler):
    """Behaves exactly like the builtin handler, but records each 429 backoff it honors."""

    def __init__(self, report: FidelityReport, **kwargs):
        super().__init__(**kwargs)
        self._report = report

    def prepare_for_next_attempt(self, *, state: RetryState, request: HttpRequest,
                                 response: HttpResponse | None = None,
                                 error: Exception | None = None) -> None:
        retry_after = None
        if response is not None:
            ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if isinstance(ra, list):
                ra = ra[0] if ra else None
            try:
                retry_after = float(ra) if ra is not None else None
            except (TypeError, ValueError):
                retry_after = None
        status = response.status_code if response is not None else 0
        op = (request.url or "").rsplit("/", 1)[-1] if request else "?"
        # honored == we have a usable Retry-After to back off on, which the base handler uses
        self._report.record_rate_limit(op, int(status or 429), retry_after,
                                        honored=retry_after is not None)
        super().prepare_for_next_attempt(state=state, request=request,
                                         response=response, error=error)


def make_web_client(token: str, cfg: SlackConfig, report: FidelityReport) -> WebClient:
    client = WebClient(token=token, base_url=cfg.base_url)
    # Production-standard retry posture: honor 429 Retry-After, retry transient conn errors.
    client.retry_handlers.append(_RecordingRateLimitHandler(report, max_retry_count=3))
    client.retry_handlers.append(ConnectionErrorRetryHandler(max_retry_count=3))
    return client


def build_authorize_url(cfg: SlackConfig, state: str = "fidelity") -> str:
    client_id, _, redirect_uri = cfg.require_oauth()
    # Request BOTH bot scopes (`scope`) and user scopes (`user_scope`): the user
    # scopes are what cause Slack to mint the xoxp user token used for DM reads.
    generator = AuthorizeUrlGenerator(
        client_id=client_id,
        scopes=list(cfg.scopes),
        user_scopes=list(cfg.user_scopes),
        redirect_uri=redirect_uri,
        authorization_url=cfg.authorize_url,
    )
    return generator.generate(state=state)


def exchange_code(cfg: SlackConfig, code: str, report: FidelityReport) -> TokenSet:
    """Exchange an OAuth code for the bot + user tokens via oauth.v2.access.

    Validates the response against the official spec and asserts the two-token
    contract: when user scopes were requested, ``authed_user.access_token`` must
    be a DISTINCT xoxp token (not the bot token). A missing/duplicate user token
    is a protocol divergence — that's exactly the failure that would let DM
    ingestion pass against a mock yet break against real Slack.
    """
    client_id, client_secret, redirect_uri = cfg.require_oauth()
    # oauth.v2.access does not require a token; base_url is the only configured knob.
    client = WebClient(base_url=cfg.base_url)
    resp = client.oauth_v2_access(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
    )
    from ..schemas import SpecValidator  # local import avoids a cycle at module load
    SpecValidator("slack").validate_response(resp.data, "/oauth.v2.access", report,
                                             label="oauth.v2.access")
    bot = resp.get("access_token")
    authed_user = resp.get("authed_user") or {}
    user = authed_user.get("access_token")
    if not bot:
        raise RuntimeError("oauth.v2.access returned no bot access_token")

    if cfg.user_scopes:
        # We asked for user scopes, so a faithful Slack returns a distinct xoxp.
        report.record_protocol(
            "oauth.two_token.user_token_present", bool(user),
            "user_scope was requested but authed_user.access_token is absent — DM "
            "ingestion would have no token" if not user else "xoxp user token issued",
        )
        if user:
            report.record_protocol(
                "oauth.two_token.tokens_distinct", user != bot,
                "authed_user.access_token equals the bot token (real Slack issues a "
                "DISTINCT xoxp user token)" if user == bot else "bot/user tokens are distinct",
            )
            report.record_protocol(
                "oauth.two_token.user_token_prefix", user.startswith("xoxp-"),
                f"user token does not start with xoxp- ({user[:6]}…)"
                if not user.startswith("xoxp-") else "user token is xoxp-",
            )
    report.auth["bot_token_prefix"] = bot[:5]
    report.auth["user_token_prefix"] = (user or "")[:5] or "(none)"
    return TokenSet(bot=bot, user=user)


def _capture_code_via_callback(cfg: SlackConfig, report: FidelityReport) -> TokenSet:
    """One-shot local HTTP server that captures the OAuth redirect's ?code=."""
    redirect = cfg.require_oauth()[2]
    parts = urlsplit(redirect)
    host, port = parts.hostname or "localhost", parts.port or 80
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            qs = parse_qs(urlsplit(self.path).query)
            if "code" in qs:
                captured["code"] = qs["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Authorization received. You can close this tab.")
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, *_):  # silence default logging
            pass

    server = HTTPServer((host, port), Handler)
    print(f"\nOpen this URL to authorize:\n  {build_authorize_url(cfg)}\n")
    print(f"Waiting for redirect on {redirect} ...")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    while "code" not in captured:
        server.handle_request()
    server.shutdown()
    return exchange_code(cfg, captured["code"], report)


def acquire_tokens(cfg: SlackConfig, report: FidelityReport) -> TokenSet:
    """Obtain the (bot, user) token pair the two-token model needs.

    Pre-issued tokens win (SLACK_BOT_TOKEN [+ SLACK_USER_TOKEN]); otherwise we run
    the real OAuth v2 flow, which mints both.
    """
    if cfg.bot_token:
        report.auth["method"] = "pre-issued tokens"
        if not cfg.user_token:
            report.note("SLACK_USER_TOKEN not set — DM (im/mpim) ingestion will be "
                        "skipped; only the channel path is exercised.")
        return TokenSet(bot=cfg.bot_token, user=cfg.user_token)
    code = os.environ.get("SLACK_OAUTH_CODE")
    if code:
        report.auth["method"] = "oauth.v2.access (code from env)"
        return exchange_code(cfg, code, report)
    report.auth["method"] = "oauth.v2.access (interactive)"
    return _capture_code_via_callback(cfg, report)
