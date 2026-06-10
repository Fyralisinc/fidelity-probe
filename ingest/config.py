"""Environment-driven configuration.

The guiding rule of this client: the *only* per-provider behavioral knob is the
base URL (and Discord's gateway URL). Everything else is identical to what you'd
ship to production. Credentials are supplied via env as normal, but there are no
mock-specific switches anywhere.

Each base URL defaults to the real production host, so running with no env at all
targets the real service. Pointing at a local mock is purely a matter of setting
the base URL env var.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

load_dotenv()  # load a .env file if present; real env always wins


class ConfigError(RuntimeError):
    """Raised when a required credential/setting is missing — fail loud, never guess."""


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ConfigError(
            f"environment variable {name} is required but not set. "
            f"See .env.example for the full list."
        )
    return val


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


# --------------------------------------------------------------------------- Slack


@dataclass(frozen=True)
class SlackConfig:
    # The single behavioral knob. slack_sdk requires a trailing slash.
    base_url: str
    # OAuth consent host. In production this is a different host from the API
    # (slack.com/oauth/... vs slack.com/api/...); we derive it from the API base's
    # origin unless explicitly overridden, so it stays "base URL only".
    authorize_url: str
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    # BOT scopes (`scope` at authorize) -> the xoxb token that reads channels.
    scopes: tuple[str, ...]
    # USER scopes (`user_scope` at authorize) -> the xoxp token that reads the
    # consenting human's 1:1 DMs (im) and group DMs (mpim). Slack forbids a bot
    # token from reading human-human DMs, so DM ingestion is a separate token.
    user_scopes: tuple[str, ...]
    # Optional pre-issued tokens: skip the interactive OAuth dance when the service
    # just hands you tokens. The OAuth path is still the primary flow.
    # bot_token = xoxb (channels); user_token = xoxp (DMs).
    bot_token: str | None
    user_token: str | None
    signing_secret: str | None

    @classmethod
    def from_env(cls) -> "SlackConfig":
        base_url = os.environ.get("SLACK_BASE_URL", "https://slack.com/api/")
        if not base_url.endswith("/"):
            base_url += "/"
        authorize_url = os.environ.get(
            "SLACK_AUTHORIZE_URL", _origin_of(base_url) + "/oauth/v2/authorize"
        )
        # Bot scopes cover public/private channels + workspace metadata. They
        # intentionally EXCLUDE im:*/mpim:* — those are user scopes, because a bot
        # token cannot read human DMs.
        scopes = tuple(
            s.strip()
            for s in os.environ.get(
                "SLACK_SCOPES",
                "channels:read,channels:history,groups:read,groups:history,"
                "users:read,team:read",
            ).split(",")
            if s.strip()
        )
        user_scopes = tuple(
            s.strip()
            for s in os.environ.get(
                "SLACK_USER_SCOPES",
                "im:read,im:history,mpim:read,mpim:history,users:read",
            ).split(",")
            if s.strip()
        )
        return cls(
            base_url=base_url,
            authorize_url=authorize_url,
            client_id=os.environ.get("SLACK_CLIENT_ID"),
            client_secret=os.environ.get("SLACK_CLIENT_SECRET"),
            redirect_uri=os.environ.get("SLACK_REDIRECT_URI"),
            scopes=scopes,
            user_scopes=user_scopes,
            bot_token=os.environ.get("SLACK_BOT_TOKEN"),
            user_token=os.environ.get("SLACK_USER_TOKEN"),
            signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
        )

    def require_oauth(self) -> tuple[str, str, str]:
        if not (self.client_id and self.client_secret and self.redirect_uri):
            raise ConfigError(
                "SLACK_CLIENT_ID, SLACK_CLIENT_SECRET and SLACK_REDIRECT_URI are "
                "required for the OAuth flow (or set SLACK_BOT_TOKEN to skip it)."
            )
        return self.client_id, self.client_secret, self.redirect_uri

    def require_signing_secret(self) -> str:
        if not self.signing_secret:
            raise ConfigError("SLACK_SIGNING_SECRET is required to verify Events API requests.")
        return self.signing_secret


# --------------------------------------------------------------------------- GitHub


@dataclass(frozen=True)
class GitHubConfig:
    base_url: str  # e.g. https://api.github.com (PyGithub real constructor param)
    app_id: str | None
    private_key: str | None  # PEM contents (or read from GITHUB_PRIVATE_KEY_PATH)
    installation_id: str | None
    webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "GitHubConfig":
        # GITHUB_API_BASE_URL is the name an integrator is typically handed (it mirrors
        # GitHub Enterprise's documented `https://HOST/api/v3` knob); GITHUB_BASE_URL is
        # PyGithub's own constructor param name. Accept either, preferring the explicit one.
        base_url = (
            os.environ.get("GITHUB_BASE_URL")
            or os.environ.get("GITHUB_API_BASE_URL")
            or "https://api.github.com"
        ).rstrip("/")
        private_key = os.environ.get("GITHUB_PRIVATE_KEY")
        key_path = os.environ.get("GITHUB_PRIVATE_KEY_PATH")
        if not private_key and key_path:
            with open(key_path, "r", encoding="utf-8") as fh:
                private_key = fh.read()
        return cls(
            base_url=base_url,
            app_id=os.environ.get("GITHUB_APP_ID"),
            private_key=private_key,
            installation_id=os.environ.get("GITHUB_INSTALLATION_ID"),
            webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"),
        )

    def require_app_auth(self) -> tuple[str, str, str]:
        """The three inputs a GitHub App integrator is handed: app id, key, installation."""
        missing = [
            name for name, val in (
                ("GITHUB_APP_ID", self.app_id),
                ("GITHUB_PRIVATE_KEY / GITHUB_PRIVATE_KEY_PATH", self.private_key),
                ("GITHUB_INSTALLATION_ID", self.installation_id),
            ) if not val
        ]
        if missing:
            raise ConfigError(
                "GitHub App authentication requires " + ", ".join(missing) + ". "
                "See .env.example for the full list."
            )
        return self.app_id, self.private_key, self.installation_id  # type: ignore[return-value]

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("GITHUB_WEBHOOK_SECRET is required to verify webhook deliveries.")
        return self.webhook_secret


# --------------------------------------------------------------------------- Discord


@dataclass(frozen=True)
class DiscordConfig:
    api_base: str  # e.g. https://discord.com/api/v10
    gateway_url: str | None  # ws gateway base; None -> discord.py's default (real service)
    bot_token: str | None

    @classmethod
    def from_env(cls) -> "DiscordConfig":
        # DISCORD_API_BASE_URL is the name an integrator is handed; DISCORD_API_BASE is
        # the repo's older name. Accept either, default to the real production host.
        api_base = (
            os.environ.get("DISCORD_API_BASE")
            or os.environ.get("DISCORD_API_BASE_URL")
            or "https://discord.com/api/v10"
        ).rstrip("/")
        return cls(
            api_base=api_base,
            gateway_url=os.environ.get("DISCORD_GATEWAY_URL"),
            bot_token=os.environ.get("DISCORD_BOT_TOKEN"),
        )

    def require_bot_token(self) -> str:
        if not self.bot_token:
            raise ConfigError(
                "DISCORD_BOT_TOKEN is required (the bot token from the Discord application)."
            )
        return self.bot_token


# --------------------------------------------------------------------------- Google

# Read-only scopes per the integration model.
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DIRECTORY_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"


@dataclass(frozen=True)
class GoogleConfig:
    """Service-account / domain-wide-delegation config shared by Gmail and Calendar.

    The only behavioral knobs are the base URLs (token, REST, directory, JWKS). All
    default to the real Google hosts. The SA private key is supplied the way a real
    service-account key is (env or file); if none is configured the client falls back
    to an ephemeral key (the mock does not verify the assertion signature) and records
    that fact.
    """
    service_account_email: str | None
    customer_id: str | None
    domain: str | None
    admin_subject: str | None
    private_key: str | None
    gmail_base: str
    gmail_token_url: str
    directory_base: str
    directory_token_url: str
    calendar_base: str
    calendar_token_url: str
    drive_base: str
    drive_token_url: str
    jwks_url: str | None
    pubsub_oidc_audience: str | None   # expected `aud` on the Pub/Sub push OIDC JWT
    pubsub_oidc_sa: str | None         # expected `email` claim (the push service account)

    @classmethod
    def from_env(cls) -> "GoogleConfig":
        domain = os.environ.get("GOOGLE_DOMAIN")
        private_key = os.environ.get("GOOGLE_PRIVATE_KEY")
        key_path = os.environ.get("GOOGLE_PRIVATE_KEY_PATH")
        if not private_key and key_path:
            with open(key_path, "r", encoding="utf-8") as fh:
                private_key = fh.read()
        return cls(
            service_account_email=os.environ.get("GOOGLE_SERVICE_ACCOUNT_EMAIL"),
            customer_id=os.environ.get("GOOGLE_CUSTOMER_ID"),
            domain=domain,
            admin_subject=os.environ.get("GOOGLE_ADMIN_SUBJECT")
            or (f"admin@{domain}" if domain else None),
            private_key=private_key,
            gmail_base=os.environ.get(
                "GMAIL_API_BASE_URL", "https://gmail.googleapis.com/gmail/v1").rstrip("/"),
            gmail_token_url=os.environ.get(
                "GMAIL_TOKEN_URL", "https://oauth2.googleapis.com/token"),
            directory_base=os.environ.get(
                "GMAIL_DIRECTORY_BASE_URL",
                "https://admin.googleapis.com/admin/directory/v1").rstrip("/"),
            directory_token_url=os.environ.get(
                "GMAIL_TOKEN_URL", "https://oauth2.googleapis.com/token"),
            calendar_base=os.environ.get(
                "CALENDAR_API_BASE_URL", "https://www.googleapis.com/calendar/v3").rstrip("/"),
            calendar_token_url=os.environ.get(
                "CALENDAR_TOKEN_URL", "https://oauth2.googleapis.com/token"),
            drive_base=os.environ.get(
                "GOOGLE_DRIVE_API_BASE_URL", "https://www.googleapis.com/drive/v3").rstrip("/"),
            drive_token_url=os.environ.get(
                "GOOGLE_DRIVE_TOKEN_URL",
                os.environ.get("GMAIL_TOKEN_URL", "https://oauth2.googleapis.com/token")),
            jwks_url=os.environ.get("GMAIL_JWKS_URL",
                                    "https://www.googleapis.com/oauth2/v3/certs"),
            pubsub_oidc_audience=os.environ.get("GMAIL_PUBSUB_OIDC_AUDIENCE"),
            pubsub_oidc_sa=os.environ.get("GMAIL_PUBSUB_OIDC_SA"),
        )

    def require_identity(self) -> tuple[str, str, str]:
        missing = [n for n, v in (("GOOGLE_SERVICE_ACCOUNT_EMAIL", self.service_account_email),
                                  ("GOOGLE_CUSTOMER_ID", self.customer_id),
                                  ("GOOGLE_DOMAIN", self.domain)) if not v]
        if missing:
            raise ConfigError("Google Workspace ingestion requires " + ", ".join(missing) + ".")
        return self.service_account_email, self.customer_id, self.domain  # type: ignore[return-value]


# --------------------------------------------------------------------------- Notion


@dataclass(frozen=True)
class NotionConfig:
    api_base: str  # e.g. https://api.notion.com
    token: str | None
    version: str  # Notion-Version header, pinned
    webhook_verification_token: str | None  # App-level HMAC secret for inbound webhooks

    @classmethod
    def from_env(cls) -> "NotionConfig":
        return cls(
            api_base=(os.environ.get("NOTION_API_BASE_URL")
                      or os.environ.get("NOTION_BASE_URL")
                      or "https://api.notion.com").rstrip("/"),
            token=os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_BOT_TOKEN"),
            version=os.environ.get("NOTION_VERSION", "2022-06-28"),
            webhook_verification_token=os.environ.get("NOTION_WEBHOOK_VERIFICATION_TOKEN"),
        )

    def require_token(self) -> str:
        if not self.token:
            raise ConfigError("NOTION_TOKEN is required (the internal integration token).")
        return self.token

    def require_webhook_verification_token(self) -> str:
        if not self.webhook_verification_token:
            raise ConfigError("NOTION_WEBHOOK_VERIFICATION_TOKEN is required for the live slice "
                              "(the App-level secret webhook events are signed with).")
        return self.webhook_verification_token


# --------------------------------------------------------------------------- QuickBooks


@dataclass(frozen=True)
class QuickBooksConfig:
    """Intuit QuickBooks Online (QBO) Accounting API v3. OAuth Bearer + realm id;
    the realm is the {realmId} path segment. base_url defaults to the production
    host; point it at the mock via QUICKBOOKS_API_BASE_URL."""
    base_url: str
    realm_id: str | None
    access_token: str | None
    minorversion: str
    webhook_verifier: str | None  # the webhook verifier token (intuit-signature HMAC key)

    @classmethod
    def from_env(cls) -> "QuickBooksConfig":
        return cls(
            base_url=(os.environ.get("QUICKBOOKS_API_BASE_URL")
                      or "https://quickbooks.api.intuit.com").rstrip("/"),
            realm_id=os.environ.get("QUICKBOOKS_REALM_ID"),
            access_token=os.environ.get("QUICKBOOKS_ACCESS_TOKEN"),
            minorversion=os.environ.get("QUICKBOOKS_MINORVERSION", "75"),
            webhook_verifier=os.environ.get("QUICKBOOKS_WEBHOOK_VERIFIER"),
        )

    def require_auth(self) -> tuple[str, str]:
        missing = [n for n, v in (("QUICKBOOKS_REALM_ID", self.realm_id),
                                  ("QUICKBOOKS_ACCESS_TOKEN", self.access_token)) if not v]
        if missing:
            raise ConfigError("QuickBooks ingestion requires " + ", ".join(missing) + ".")
        return self.realm_id, self.access_token  # type: ignore[return-value]

    def require_webhook_verifier(self) -> str:
        if not self.webhook_verifier:
            raise ConfigError("QUICKBOOKS_WEBHOOK_VERIFIER is required to verify webhooks.")
        return self.webhook_verifier


# --------------------------------------------------------------------------- Jira


@dataclass(frozen=True)
class JiraConfig:
    """Atlassian Cloud Jira. Unlike the others there's no global host: each install has
    its own site base URL (https://<site>.atlassian.net). Auth is HTTP Basic with
    base64(account_email:api_token)."""
    base_url: str | None  # the site base, e.g. https://acme.atlassian.net (or the mock)
    account_email: str | None
    api_token: str | None
    webhook_secret: str | None  # the secret a dynamic webhook is registered with (HMAC key)

    @classmethod
    def from_env(cls) -> "JiraConfig":
        # JIRA_API_BASE_URL overrides the per-install site base (used to point at the mock);
        # otherwise the per-install JIRA_BASE_URL site is used. No global production default.
        base = os.environ.get("JIRA_API_BASE_URL") or os.environ.get("JIRA_BASE_URL")
        return cls(
            base_url=base.rstrip("/") if base else None,
            account_email=os.environ.get("JIRA_ACCOUNT_EMAIL"),
            api_token=os.environ.get("JIRA_API_TOKEN"),
            webhook_secret=os.environ.get("JIRA_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> tuple[str, str, str]:
        missing = [n for n, v in (("JIRA_API_BASE_URL/JIRA_BASE_URL", self.base_url),
                                  ("JIRA_ACCOUNT_EMAIL", self.account_email),
                                  ("JIRA_API_TOKEN", self.api_token)) if not v]
        if missing:
            raise ConfigError("Jira ingestion requires " + ", ".join(missing) + ".")
        return self.base_url, self.account_email, self.api_token  # type: ignore[return-value]

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("JIRA_WEBHOOK_SECRET is required to verify webhook deliveries.")
        return self.webhook_secret


# --------------------------------------------------------------------------- Grafana


@dataclass(frozen=True)
class GrafanaConfig:
    """Grafana observability. One org-scoped service-account Bearer token
    (``Authorization: Bearer glsa_…``) reads the whole org's annotations; the same
    instance also signs its Alerting webhook with an HMAC secret. base_url defaults
    to nothing — point it at the instance (or the mock) via GRAFANA_API_BASE_URL."""
    base_url: str | None      # https://<instance>.grafana.net (or the mock)
    api_token: str | None     # service-account token (glsa_…)
    webhook_secret: str | None  # HMAC-SHA256 alerting-webhook secret

    @classmethod
    def from_env(cls) -> "GrafanaConfig":
        base = os.environ.get("GRAFANA_API_BASE_URL")
        return cls(
            base_url=base.rstrip("/") if base else None,
            api_token=os.environ.get("GRAFANA_API_TOKEN"),
            webhook_secret=os.environ.get("GRAFANA_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> tuple[str, str]:
        missing = [n for n, v in (("GRAFANA_API_BASE_URL", self.base_url),
                                  ("GRAFANA_API_TOKEN", self.api_token)) if not v]
        if missing:
            raise ConfigError("Grafana ingestion requires " + ", ".join(missing) + ".")
        return self.base_url, self.api_token  # type: ignore[return-value]

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("GRAFANA_WEBHOOK_SECRET is required to verify webhook deliveries.")
        return self.webhook_secret


# --------------------------------------------------------------------------- Mercury


@dataclass(frozen=True)
class MercuryConfig:
    """Mercury (business banking) REST API. The org API token authenticates via
    ``Authorization: Bearer <token>`` (token carries a literal ``secret-token:``
    prefix). base_url defaults to the production host; point it at the mock via
    MERCURY_API_BASE_URL (note the real base includes the ``/api/v1`` version
    segment)."""
    base_url: str
    api_token: str | None
    webhook_secret: str | None  # the endpoint's secretKey (Mercury-Signature HMAC key)

    @classmethod
    def from_env(cls) -> "MercuryConfig":
        return cls(
            base_url=(os.environ.get("MERCURY_API_BASE_URL")
                      or "https://api.mercury.com/api/v1").rstrip("/"),
            api_token=os.environ.get("MERCURY_API_TOKEN"),
            webhook_secret=os.environ.get("MERCURY_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> tuple[str, str]:
        if not self.api_token:
            raise ConfigError("MERCURY_API_TOKEN is required (the org API token).")
        return self.base_url, self.api_token

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("MERCURY_WEBHOOK_SECRET is required to verify webhook deliveries "
                              "(the endpoint's secretKey).")
        return self.webhook_secret


# --------------------------------------------------------------------------- Ashby


@dataclass(frozen=True)
class AshbyConfig:
    """Ashby (recruiting / ATS) RPC API. Auth is the org API key presented as the
    HTTP Basic *username* with an EMPTY password (``Authorization: Basic
    base64("<key>:")``). base_url defaults to the production host (no version path
    — Ashby versions via the ``Accept: application/json; version=1`` header, not
    the URL); point it at the mock via ASHBY_API_BASE_URL."""
    base_url: str
    api_key: str | None
    webhook_secret: str | None  # the webhook's signing secret (Ashby-Signature HMAC key)

    @classmethod
    def from_env(cls) -> "AshbyConfig":
        return cls(
            base_url=(os.environ.get("ASHBY_API_BASE_URL")
                      or "https://api.ashbyhq.com").rstrip("/"),
            api_key=os.environ.get("ASHBY_API_KEY"),
            webhook_secret=os.environ.get("ASHBY_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> tuple[str, str]:
        if not self.api_key:
            raise ConfigError("ASHBY_API_KEY is required (the org API key; sent as the "
                              "HTTP Basic username with an empty password).")
        return self.base_url, self.api_key

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("ASHBY_WEBHOOK_SECRET is required to verify webhook "
                              "deliveries (the webhook's signing secret).")
        return self.webhook_secret


# --------------------------------------------------------------------------- Brex


@dataclass(frozen=True)
class BrexConfig:
    """Brex (corporate cards + cash management) REST API. Auth is a user/OAuth
    token via ``Authorization: Bearer <token>`` (user tokens carry a ``bxt_``
    prefix). base_url defaults to the production host ``https://api.brex.com``
    (the ``/v2`` segment is part of each path, NOT the base); point it at the mock
    via BREX_API_BASE_URL. The webhook secret is the Svix ``whsec_…`` signing
    secret from ``GET /v1/webhooks/secrets``."""
    base_url: str
    api_token: str | None
    webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "BrexConfig":
        return cls(
            base_url=(os.environ.get("BREX_API_BASE_URL")
                      or "https://api.brex.com").rstrip("/"),
            api_token=os.environ.get("BREX_API_TOKEN"),
            webhook_secret=os.environ.get("BREX_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> tuple[str, str]:
        if not self.api_token:
            raise ConfigError("BREX_API_TOKEN is required (a Brex user/OAuth token, "
                              "sent as Authorization: Bearer <token>).")
        return self.base_url, self.api_token

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("BREX_WEBHOOK_SECRET is required to verify webhook "
                              "deliveries (the Svix whsec_ signing secret).")
        return self.webhook_secret


# --------------------------------------------------------------------------- Deel


@dataclass(frozen=True)
class DeelConfig:
    """Deel (global payroll / contractor payments) REST API. Auth is a long-lived
    org/personal API token via ``Authorization: Bearer <token>``. base_url defaults
    to the production host ``https://api.letsdeel.com/rest/v2`` (the ``/rest/v2``
    segment IS part of the base; the client does ``{base}/contracts``); point it at
    the mock via DEEL_API_BASE_URL. The webhook secret is the HMAC signing key used
    to verify the ``x-deel-signature`` header."""
    base_url: str
    api_token: str | None
    webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "DeelConfig":
        return cls(
            base_url=(os.environ.get("DEEL_API_BASE_URL")
                      or "https://api.letsdeel.com/rest/v2").rstrip("/"),
            api_token=os.environ.get("DEEL_API_TOKEN"),
            webhook_secret=os.environ.get("DEEL_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> tuple[str, str]:
        if not self.api_token:
            raise ConfigError("DEEL_API_TOKEN is required (a Deel org/personal API "
                              "token, sent as Authorization: Bearer <token>).")
        return self.base_url, self.api_token

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("DEEL_WEBHOOK_SECRET is required to verify webhook "
                              "deliveries (the x-deel-signature HMAC key).")
        return self.webhook_secret


# --------------------------------------------------------------------------- HiBob


@dataclass(frozen=True)
class HibobConfig:
    """HiBob ("Bob") HR-platform REST API. Auth is a **service user** HTTP Basic
    credential ``base64(service_user_id:token)`` (apidocs.hibob.com/reference/
    authorization). base_url defaults to the production host
    ``https://api.hibob.com`` (the ``/v1`` segment is part of each path, NOT the
    base); point it at the mock via HIBOB_API_BASE_URL. The webhook secret is the
    HMAC-SHA512 signing key used to verify the ``Bob-Signature`` header."""
    base_url: str
    service_user_id: str | None
    service_user_token: str | None
    webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "HibobConfig":
        return cls(
            base_url=(os.environ.get("HIBOB_API_BASE_URL")
                      or "https://api.hibob.com").rstrip("/"),
            service_user_id=os.environ.get("HIBOB_SERVICE_USER_ID"),
            service_user_token=os.environ.get("HIBOB_SERVICE_USER_TOKEN"),
            webhook_secret=os.environ.get("HIBOB_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> tuple[str, str, str]:
        if not (self.service_user_id and self.service_user_token):
            raise ConfigError("HIBOB_SERVICE_USER_ID and HIBOB_SERVICE_USER_TOKEN "
                              "are required (the HiBob service-user Basic credential, "
                              "sent as Authorization: Basic base64(id:token)).")
        return self.base_url, self.service_user_id, self.service_user_token

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("HIBOB_WEBHOOK_SECRET is required to verify webhook "
                              "deliveries (the Bob-Signature HMAC-SHA512 key).")
        return self.webhook_secret


# --------------------------------------------------------------------------- Figma


@dataclass(frozen=True)
class FigmaConfig:
    """Figma design-tool REST API. Auth is a personal/plan access token presented as
    the ``X-Figma-Token`` header (the OAuth ``Authorization: Bearer`` form is also
    accepted on read endpoints). base_url defaults to the production host
    ``https://api.figma.com`` (the ``/v1`` segment is part of each path, NOT the
    base); point it at the mock via FIGMA_API_BASE_URL. ``team_id`` is the root of
    the teams → projects → files enumeration. The webhook ``passcode`` is the
    Webhooks-v2 plaintext body passcode (NOT an HMAC secret — Figma signs nothing)."""
    base_url: str
    team_id: str | None
    access_token: str | None
    webhook_passcode: str | None

    @classmethod
    def from_env(cls) -> "FigmaConfig":
        return cls(
            base_url=(os.environ.get("FIGMA_API_BASE_URL")
                      or "https://api.figma.com").rstrip("/"),
            team_id=os.environ.get("FIGMA_TEAM_ID"),
            access_token=os.environ.get("FIGMA_ACCESS_TOKEN"),
            webhook_passcode=os.environ.get("FIGMA_WEBHOOK_PASSCODE"),
        )

    def require_auth(self) -> tuple[str, str, str]:
        missing = [n for n, v in (("FIGMA_TEAM_ID", self.team_id),
                                  ("FIGMA_ACCESS_TOKEN", self.access_token)) if not v]
        if missing:
            raise ConfigError("Figma ingestion requires " + ", ".join(missing) + " "
                              "(the team to enumerate + the X-Figma-Token access token).")
        return self.base_url, self.team_id, self.access_token  # type: ignore[return-value]

    def require_webhook_passcode(self) -> str:
        if not self.webhook_passcode:
            raise ConfigError("FIGMA_WEBHOOK_PASSCODE is required for the live slice "
                              "(the Webhooks-v2 plaintext body passcode — Figma has no HMAC).")
        return self.webhook_passcode


# --------------------------------------------------------------------------- Miro


@dataclass(frozen=True)
class MiroConfig:
    """Miro collaborative-whiteboard REST API v2. Auth is a single long-lived
    org-level app token presented as ``Authorization: Bearer <token>`` (scope
    ``boards:read``). base_url defaults to the production host
    ``https://api.miro.com/v2`` (the ``/v2`` segment IS part of the base); point it
    at the mock via MIRO_API_BASE_URL. ``org_id`` namespaces every observation's
    ``external_id`` (miro:{org_id}:item:{item_id}:{version}). Miro is POLL-ONLY —
    its experimental webhooks were discontinued 2025-12-05, so there is no webhook
    secret and no live slice."""
    base_url: str
    org_id: str | None
    access_token: str | None

    @classmethod
    def from_env(cls) -> "MiroConfig":
        return cls(
            base_url=(os.environ.get("MIRO_API_BASE_URL")
                      or "https://api.miro.com/v2").rstrip("/"),
            org_id=os.environ.get("MIRO_ORG_ID"),
            access_token=os.environ.get("MIRO_ACCESS_TOKEN"),
        )

    def require_auth(self) -> tuple[str, str, str]:
        missing = [n for n, v in (("MIRO_ORG_ID", self.org_id),
                                  ("MIRO_ACCESS_TOKEN", self.access_token)) if not v]
        if missing:
            raise ConfigError("Miro ingestion requires " + ", ".join(missing) + " "
                              "(the org id that namespaces external_ids + the "
                              "org-app Bearer token).")
        return self.base_url, self.org_id, self.access_token  # type: ignore[return-value]


# --------------------------------------------------------------------------- Ramp


@dataclass(frozen=True)
class RampConfig:
    """Ramp (corporate cards + bill-pay + reimbursements) Developer API. Auth is
    OAuth 2.0: a client-credentials ``client_id``/``client_secret`` is exchanged at
    ``POST /developer/v1/token`` for a Bearer access token (``ramp_business_tok_…``);
    if a pre-minted ``RAMP_ACCESS_TOKEN`` is supplied the exchange is skipped.
    base_url defaults to the production host ``https://api.ramp.com`` (the
    ``/developer/v1`` segment is part of each path, NOT the base); point it at the
    mock via RAMP_API_BASE_URL. The webhook secret is the HMAC-SHA256 signing key
    used to verify the ``X-Ramp-Signature`` header."""
    base_url: str
    client_id: str | None
    client_secret: str | None
    access_token: str | None
    webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "RampConfig":
        return cls(
            base_url=(os.environ.get("RAMP_API_BASE_URL")
                      or "https://api.ramp.com").rstrip("/"),
            client_id=os.environ.get("RAMP_CLIENT_ID"),
            client_secret=os.environ.get("RAMP_CLIENT_SECRET"),
            access_token=os.environ.get("RAMP_ACCESS_TOKEN"),
            webhook_secret=os.environ.get("RAMP_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> str:
        """Either a pre-minted access token OR client-credentials must be present."""
        if self.access_token:
            return self.base_url
        if not (self.client_id and self.client_secret):
            raise ConfigError(
                "Ramp ingestion requires RAMP_ACCESS_TOKEN, or RAMP_CLIENT_ID + "
                "RAMP_CLIENT_SECRET to mint one via the client-credentials grant.")
        return self.base_url

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("RAMP_WEBHOOK_SECRET is required to verify webhook "
                              "deliveries (the X-Ramp-Signature HMAC-SHA256 key).")
        return self.webhook_secret


# --------------------------------------------------------------------------- Gusto


@dataclass(frozen=True)
class GustoConfig:
    """Gusto (payroll + HR) Embedded Payroll API. Auth is OAuth 2.0 Bearer — the
    install is operator-mediated (the operator pastes the ``company_uuid`` +
    ``access_token`` from their own Gusto OAuth app; there is NO client-credentials
    grant). If a ``GUSTO_REFRESH_TOKEN`` (+ client creds) is supplied instead of a
    token, the slice mints one at ``POST /oauth/token``. base_url defaults to the
    production host ``https://api.gusto.com`` (the ``/v1`` segment is part of each
    path); point it at the mock via GUSTO_API_BASE_URL. The webhook secret is the
    subscription's ``verification_token`` (the X-Gusto-Signature HMAC-SHA256 key)."""
    base_url: str
    company_uuid: str | None
    access_token: str | None
    refresh_token: str | None
    client_id: str | None
    client_secret: str | None
    webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "GustoConfig":
        return cls(
            base_url=(os.environ.get("GUSTO_API_BASE_URL")
                      or "https://api.gusto.com").rstrip("/"),
            company_uuid=os.environ.get("GUSTO_COMPANY_UUID"),
            access_token=os.environ.get("GUSTO_ACCESS_TOKEN"),
            refresh_token=os.environ.get("GUSTO_REFRESH_TOKEN"),
            client_id=os.environ.get("GUSTO_CLIENT_ID"),
            client_secret=os.environ.get("GUSTO_CLIENT_SECRET"),
            webhook_secret=os.environ.get("GUSTO_WEBHOOK_SECRET"),
        )

    def require_auth(self) -> str:
        if not self.company_uuid:
            raise ConfigError("Gusto ingestion requires GUSTO_COMPANY_UUID.")
        if not (self.access_token or (self.refresh_token and self.client_id
                                      and self.client_secret)):
            raise ConfigError(
                "Gusto ingestion requires GUSTO_ACCESS_TOKEN, or GUSTO_REFRESH_TOKEN "
                "+ GUSTO_CLIENT_ID + GUSTO_CLIENT_SECRET to mint one.")
        return self.base_url

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise ConfigError("GUSTO_WEBHOOK_SECRET is required to verify webhook "
                              "deliveries (the X-Gusto-Signature HMAC-SHA256 key = the "
                              "subscription's verification_token).")
        return self.webhook_secret


# --------------------------------------------------------------------------- Shared


@dataclass(frozen=True)
class WebhookConfig:
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "WebhookConfig":
        return cls(
            host=os.environ.get("WEBHOOK_HOST", "0.0.0.0"),
            port=int(os.environ.get("WEBHOOK_PORT", "8080")),
        )
