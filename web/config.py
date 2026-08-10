"""
Portal configuration, read once from environment variables. There is no
.env/config.json convention in this repo (runtime settings normally live in
db/settings.sqlite key-value tables) -- the portal follows the same pattern
DISCORD_BOT_TOKEN already uses: plain os.getenv, read at process start.
"""
import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger('bot')


@dataclass(frozen=True)
class PortalConfig:
    port: int
    base_url: str
    signing_secret: str


def _env_truthy(value: Optional[str], default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def load_portal_config() -> Optional[PortalConfig]:
    """Read the portal's env vars. Returns None (and logs why) when the
    portal is disabled or missing required config, so the caller can skip
    starting the web server instead of starting it insecurely."""
    if not _env_truthy(os.getenv("PORTAL_ENABLED"), default=True):
        logger.info("Minister portal disabled via PORTAL_ENABLED")
        return None

    base_url = (os.getenv("PORTAL_BASE_URL") or "").strip().rstrip("/")
    signing_secret = os.getenv("PORTAL_SIGNING_SECRET") or ""
    port_str = os.getenv("PORTAL_PORT") or "8090"

    if not base_url:
        logger.error("PORTAL_ENABLED is set but PORTAL_BASE_URL is missing -- portal will not start")
        return None
    if not signing_secret:
        logger.error("PORTAL_ENABLED is set but PORTAL_SIGNING_SECRET is missing -- portal will not start")
        return None

    try:
        port = int(port_str)
    except ValueError:
        logger.error(f"PORTAL_PORT={port_str!r} is not a valid integer -- portal will not start")
        return None

    return PortalConfig(port=port, base_url=base_url, signing_secret=signing_secret)
