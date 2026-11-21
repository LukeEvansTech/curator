"""Settings from the environment. Every value has a default that works on a laptop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("CURATOR_DATA", "data")))
    public_url: str = field(default_factory=lambda: _env("CURATOR_PUBLIC_URL").rstrip("/"))
    timezone: str = field(default_factory=lambda: _env("CURATOR_TZ", "Europe/London"))
    # Where the app reaches the two services' APIs (in-cluster URLs on the cluster).
    radarr_url: str = field(default_factory=lambda: _env("RADARR_URL").rstrip("/"))
    radarr_key: str = field(default_factory=lambda: _env("RADARR_API_KEY"))
    sonarr_url: str = field(default_factory=lambda: _env("SONARR_URL").rstrip("/"))
    sonarr_key: str = field(default_factory=lambda: _env("SONARR_API_KEY"))
    # Where a browser reaches the two services' web UIs, for "open in Radarr" links. Empty: no link.
    radarr_link: str = field(default_factory=lambda: _env("RADARR_LINK_URL").rstrip("/"))
    sonarr_link: str = field(default_factory=lambda: _env("SONARR_LINK_URL").rstrip("/"))
    # The allow list is read from the two services on this schedule, and on demand from /ops.
    allow_schedule: str = field(
        default_factory=lambda: _env("CURATOR_ALLOW_SCHEDULE", "23 */6 * * *")
    )
    # How long a deleted file sits in the services' recycle bin before it is purged.
    recycle_days: int = field(default_factory=lambda: _int("CURATOR_RECYCLE_DAYS", 14))
    # A document older than this many days is shown as stale on the pages.
    stale_days: int = field(default_factory=lambda: _int("CURATOR_STALE_DAYS", 8))
    # Judge cost estimate, US dollars per million tokens (list price of the judge's model).
    price_in: float = field(default_factory=lambda: _float("CURATOR_PRICE_IN_PER_MTOK", 4.0))
    price_out: float = field(default_factory=lambda: _float("CURATOR_PRICE_OUT_PER_MTOK", 20.0))
    max_publish_bytes: int = field(
        default_factory=lambda: _int("CURATOR_MAX_PUBLISH_BYTES", 16 * 1024 * 1024)
    )
    version: str = field(default_factory=lambda: _env("APP_VERSION", "0.0.0-dev"))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "curator.db"

    def key(self, library: str) -> str:
        """The API key a library's documents are signed with: the service's own."""
        return {"film": self.radarr_key, "tv": self.sonarr_key}.get(library, "")

    def link_base(self, library: str) -> str:
        return {"film": self.radarr_link, "tv": self.sonarr_link}.get(library, "")

    def link(self, path: str) -> str:
        return f"{self.public_url}{path}" if self.public_url else path


def load() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
