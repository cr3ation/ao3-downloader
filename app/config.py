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
    user_agent: str
    max_results_cap: int
    max_pages: int
    epub_tag: str
    flat_downloads: bool

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
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "60")),
            downloads_dir=Path(os.getenv("DOWNLOADS_DIR", "/app/downloads")),
            user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
            max_results_cap=int(os.getenv("MAX_RESULTS_CAP", "500")),
            max_pages=int(os.getenv("MAX_PAGES", "50")),
            epub_tag=os.getenv("EPUB_TAG", "Fanfiction").strip(),
            flat_downloads=os.getenv("FLAT_DOWNLOADS", "false").strip().lower() in ("1", "true", "yes"),
        )
