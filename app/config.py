"""Application settings, read once from environment variables at startup."""
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Settings:
    base_url: str
    min_delay: float
    max_delay: float
    max_retries: int
    backoff_base: float
    backoff_cap: float
    retry_after_cap: float
    request_timeout: float
    downloads_dir: Path
    config_dir: Path
    db_path: Path
    session_cookie_secure: str  # "auto" | "true" | "false"
    session_ttl_days: int
    oidc_state_ttl: int
    public_base_url: str
    login_max_attempts: int
    login_lockout_seconds: int
    user_agent: str
    max_results_cap: int
    max_pages: int
    epub_tag: str
    flat_downloads: bool
    cover_generation_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            base_url=os.getenv("AO3_BASE_URL", "https://archiveofourown.org").rstrip("/"),
            min_delay=float(os.getenv("AO3_MIN_DELAY", "3")),
            max_delay=float(os.getenv("AO3_MAX_DELAY", "6")),
            max_retries=int(os.getenv("MAX_RETRIES", "5")),
            backoff_base=float(os.getenv("RETRY_BACKOFF_BASE", "30")),
            backoff_cap=float(os.getenv("RETRY_BACKOFF_CAP", "600")),
            retry_after_cap=float(os.getenv("RETRY_AFTER_CAP", "1800")),
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "120")),
            downloads_dir=Path(os.getenv("DOWNLOADS_DIR", "/app/downloads")),
            # Kept out of downloads_dir so Calibre's watched folder holds only e-books.
            config_dir=Path(os.getenv("CONFIG_DIR", "/app/config")),
            db_path=Path(os.getenv("DB_PATH", "")) if os.getenv("DB_PATH") else Path(os.getenv("CONFIG_DIR", "/app/config")) / "app.db",
            # "auto" sets the Secure flag only over HTTPS. Hardcoding it true would
            # make the browser silently drop the cookie on a plain-HTTP LAN, which
            # looks exactly like a wrong password.
            session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "auto").strip().lower(),
            session_ttl_days=int(os.getenv("SESSION_TTL_DAYS", "30")),
            oidc_state_ttl=int(os.getenv("OIDC_STATE_TTL", "600")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            login_max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "10")),
            login_lockout_seconds=int(os.getenv("LOGIN_LOCKOUT_SECONDS", "900")),
            user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
            max_results_cap=int(os.getenv("MAX_RESULTS_CAP", "500")),
            max_pages=int(os.getenv("MAX_PAGES", "50")),
            epub_tag=os.getenv("EPUB_TAG", "Fanfiction").strip(),
            flat_downloads=os.getenv("FLAT_DOWNLOADS", "false").strip().lower() in ("1", "true", "yes"),
            cover_generation_enabled=os.getenv("COVER_GENERATION_ENABLED", "true").strip().lower()
            in ("1", "true", "yes"),
        )
