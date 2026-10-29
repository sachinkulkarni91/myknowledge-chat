"""Settings, read from environment variables (optionally from a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """A required setting is missing."""


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _hosts(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name, default)
    return tuple(p.strip().lower().lstrip(".") for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class Settings:
    # Connection settings for the MyKnowledge agent (none of these are secrets).
    tenant_id: str = ""
    client_id: str = ""
    environment_id: str = ""
    agent_schema_name: str = ""
    token_scope: str = "https://api.powerplatform.com/.default"

    # The dedicated service account the agent is called as. Username only; the
    # password is typed into the Microsoft sign-in window by login.py and is
    # never stored by this app.
    service_account_username: str = ""
    token_cache_path: str = "./.secrets/msal_cache.bin"

    # Development aid: serve fake agent activities so the UI works offline.
    mock_agent: bool = False
    mock_delay_seconds: float = 0.5

    # Links to these hosts are removed from answers, because visitors cannot
    # open them (the knowledge articles live on internal SharePoint).
    strip_link_hosts: tuple[str, ...] = ("sharepoint.com",)

    max_question_chars: int = 2000

    # Serve Swagger UI at /docs. Off by default; for local testing only.
    enable_docs: bool = False

    # Shared password for every page and API call (HTTP Basic auth, any username).
    # Empty = no password. REQUIRE_APP_PASSWORD makes an empty one a startup error.
    app_password: str = ""
    require_app_password: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            tenant_id=env("TENANT_ID", "").strip(),
            client_id=env("CLIENT_ID", "").strip(),
            environment_id=env("ENVIRONMENT_ID", "").strip(),
            agent_schema_name=env("AGENT_SCHEMA_NAME", "").strip(),
            token_scope=env("TOKEN_SCOPE", cls.token_scope).strip(),
            service_account_username=env("SERVICE_ACCOUNT_USERNAME", "").strip(),
            token_cache_path=env("TOKEN_CACHE_PATH", cls.token_cache_path).strip(),
            mock_agent=_flag("MOCK_AGENT"),
            mock_delay_seconds=float(env("MOCK_DELAY_SECONDS", cls.mock_delay_seconds)),
            strip_link_hosts=_hosts("STRIP_LINK_HOSTS", "sharepoint.com"),
            max_question_chars=int(env("MAX_QUESTION_CHARS", cls.max_question_chars)),
            enable_docs=_flag("ENABLE_DOCS"),
            app_password=env("APP_PASSWORD", ""),
            require_app_password=_flag("REQUIRE_APP_PASSWORD"),
        )

    def require_real_agent_config(self) -> None:
        required = {
            "TENANT_ID": self.tenant_id,
            "CLIENT_ID": self.client_id,
            "ENVIRONMENT_ID": self.environment_id,
            "AGENT_SCHEMA_NAME": self.agent_schema_name,
            "SERVICE_ACCOUNT_USERNAME": self.service_account_username,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError(
                "Missing settings: " + ", ".join(missing) + ". Copy .env.example to .env and "
                "fill them in, or set MOCK_AGENT=true to try the UI without the agent."
            )
