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

    @classmethod
    def from_env(cls) -> "NotionConfig":
        return cls(
            api_base=(os.environ.get("NOTION_API_BASE_URL")
                      or os.environ.get("NOTION_BASE_URL")
                      or "https://api.notion.com").rstrip("/"),
            token=os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_BOT_TOKEN"),
            version=os.environ.get("NOTION_VERSION", "2022-06-28"),
        )

    def require_token(self) -> str:
        if not self.token:
            raise ConfigError("NOTION_TOKEN is required (the internal integration token).")
        return self.token


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
