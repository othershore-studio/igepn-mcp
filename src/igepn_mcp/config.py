"""Runtime settings, all from environment variables (no secrets except the optional HTTP bearer)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER_AGENT = "igepn-mcp/0.1 (+https://github.com/othershore-studio/igepn-mcp)"
DEFAULT_CHANNEL = "SismosVolcanesIGEPN"


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(os.environ.get("IGEPN_DB", "data/igepn.db")))
    channel: str = field(default_factory=lambda: os.environ.get("IGEPN_CHANNEL", DEFAULT_CHANNEL))
    user_agent: str = field(default_factory=lambda: os.environ.get("IGEPN_USER_AGENT", DEFAULT_USER_AGENT))
    fetch_timeout: float = field(default_factory=lambda: float(os.environ.get("IGEPN_FETCH_TIMEOUT", "20")))
    # Catch-up cap: how many preview pages (20 posts each) one poll may walk back to reach the watermark.
    max_catchup_pages: int = field(default_factory=lambda: int(os.environ.get("IGEPN_MAX_CATCHUP_PAGES", "25")))
    # >0 makes `serve` poll in the background (no timer needed; 3 is the intended cadence).
    poll_minutes: float = field(default_factory=lambda: float(os.environ.get("IGEPN_POLL_MINUTES", "0")))
    # Bearer token required on streamable-http when set (an MCP proxy sends it as a header).
    http_token: str | None = field(default_factory=lambda: os.environ.get("IGEPN_MCP_TOKEN") or None)
