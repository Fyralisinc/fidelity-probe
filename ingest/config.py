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
    scopes: tuple[str, ...]
    # Optional pre-issued bot token: lets you skip the interactive OAuth dance when
    # the mock/real service just hands you a token. The OAuth path is still the
    # primary, fully-implemented flow.
    bot_token: str | None
    signing_secret: str | None

    @classmethod
    def from_env(cls) -> "SlackConfig":
        base_url = os.environ.get("SLACK_BASE_URL", "https://slack.com/api/")
        if not base_url.endswith("/"):
            base_url += "/"
        authorize_url = os.environ.get(
            "SLACK_AUTHORIZE_URL", _origin_of(base_url) + "/oauth/v2/authorize"
        )
        scopes = tuple(
            s.strip()
            for s in os.environ.get(
                "SLACK_SCOPES",
                "channels:read,channels:history,groups:read,groups:history,"
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
            bot_token=os.environ.get("SLACK_BOT_TOKEN"),
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
