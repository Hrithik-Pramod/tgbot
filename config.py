"""
Configuration, loaded from the environment.

Three bot tokens, one per role (decision D3). One codebase and one database
serve all three: separate tokens exist so that Telegram shows each party only
its own command menu, and so a leaked supplier token cannot reach the Bridge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


def _req(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def _opt(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


@dataclass(frozen=True)
class Config:
    # --- Telegram -----------------------------------------------------------
    bridge_bot_token: str
    supplier_bot_token: str
    client_bot_token: str

    # The Bridge's own Telegram user id. Retained as a reference, and used only
    # when strict_bridge_user is on.
    bridge_user_id: int

    # Authorisation is by chat membership, not by individual user
    # (client decision, 7 Sep 2026). Turning this on restores per-user checking
    # for the Bridge bot alone, without a code change.
    strict_bridge_user: bool

    # The channel that receives deposit notifications and trade summaries.
    bridge_channel_id: int

    # --- Database -----------------------------------------------------------
    database_url: str

    # --- TRON ---------------------------------------------------------------
    # B7: the client uses the free TronScan API and has run it successfully
    # before. TronGrid is configured as an automatic fallback so a rate-limit
    # or outage does not blind the monitor.
    tronscan_api_base: str
    trongrid_api_base: str
    trongrid_api_key: str
    usdt_contract: str
    poll_interval_seconds: int

    # --- Behaviour ----------------------------------------------------------
    # C5: warn when the rate in force for a pairing is older than this.
    rate_staleness_hours: int
    # E1: tolerance, expressed in USDT and converted at the trade's sell rate.
    tolerance_usdt: Decimal

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bridge_bot_token=_req("BRIDGE_BOT_TOKEN"),
            supplier_bot_token=_req("SUPPLIER_BOT_TOKEN"),
            client_bot_token=_req("CLIENT_BOT_TOKEN"),
            bridge_user_id=int(_opt("BRIDGE_USER_ID", "0")),
            strict_bridge_user=_opt("STRICT_BRIDGE_USER", "false").lower()
            in ("1", "true", "yes"),
            bridge_channel_id=int(_req("BRIDGE_CHANNEL_ID")),
            database_url=_req("DATABASE_URL"),
            tronscan_api_base=_opt(
                "TRONSCAN_API_BASE", "https://apilist.tronscanapi.com/api"
            ),
            trongrid_api_base=_opt("TRONGRID_API_BASE", "https://api.trongrid.io"),
            trongrid_api_key=_opt("TRONGRID_API_KEY", ""),
            # TRC20 USDT (Tether) contract on TRON mainnet.
            usdt_contract=_opt(
                "USDT_CONTRACT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
            ),
            poll_interval_seconds=int(_opt("POLL_INTERVAL_SECONDS", "20")),
            rate_staleness_hours=int(_opt("RATE_STALENESS_HOURS", "24")),
            tolerance_usdt=Decimal(_opt("TOLERANCE_USDT", "1")),
        )
