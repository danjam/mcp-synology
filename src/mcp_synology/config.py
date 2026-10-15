"""Settings from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import IntEnum


class Tier(IntEnum):
    """Permission tiers. Each tier includes the ones below it."""

    READ = 0
    WRITE = 1
    DESTRUCTIVE = 2


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    host: str
    port: str
    username: str
    password: str = field(repr=False)
    https: bool
    verify_ssl: bool
    tier: Tier

    @classmethod
    def from_env(cls) -> Settings:
        username = os.environ.get("SYNOLOGY_USER", "")
        password = os.environ.get("SYNOLOGY_PASSWORD", "")
        host = os.environ.get("SYNOLOGY_HOST", "")
        missing = [n for n, v in (("SYNOLOGY_HOST", host), ("SYNOLOGY_USER", username),
                                  ("SYNOLOGY_PASSWORD", password)) if not v]
        if missing:
            raise SystemExit(f"mcp-synology: missing required env vars: {', '.join(missing)}")

        https = _bool("SYNOLOGY_HTTPS", True)
        port = os.environ.get("SYNOLOGY_PORT") or ("5001" if https else "5000")

        tier_name = os.environ.get("SYNOLOGY_PERMISSION", "read").strip().upper()
        try:
            tier = Tier[tier_name]
        except KeyError:
            raise SystemExit(f"mcp-synology: SYNOLOGY_PERMISSION must be read|write|destructive, got {tier_name.lower()!r}")
        if tier is Tier.DESTRUCTIVE and not _bool("SYNOLOGY_ALLOW_DESTRUCTIVE", False):
            raise SystemExit("mcp-synology: SYNOLOGY_PERMISSION=destructive also requires SYNOLOGY_ALLOW_DESTRUCTIVE=1")

        return cls(host=host, port=port, username=username, password=password, https=https,
                   verify_ssl=_bool("SYNOLOGY_VERIFY_SSL", True), tier=tier)
