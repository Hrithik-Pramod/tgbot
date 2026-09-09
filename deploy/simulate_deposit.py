"""
Simulate a supplier deposit without putting real USDT on the chain.

WHY THIS EXISTS
---------------
deploy/simulate-trade.sql writes a trade straight into the database. That is
enough to test the client half — pastes, /done, the near-completion alert — but
it bypasses the code the real system actually runs, so it proves nothing about
the Bridge half: no deposit notification, no Confirm button, no payment slots
issued to the client.

This script fakes only the network call. Everything downstream is the real
path: record_deposit, the non-internal-wallet branch, on_supplier_deposit, the
rate lookup, the stale-rate warning, the arithmetic, and the Bridge
notification with its Confirm button attached. If it works here it works when
a real transfer arrives; the only untested piece left is whether TronScan
reports the transfer, which the contract tests in tests/test_tronscan_contract
already pin.

USAGE (inside the running container, so it shares the same .env and database)

    docker compose exec bot python deploy/simulate_deposit.py --amount 5000

    docker compose exec bot python deploy/simulate_deposit.py \
        --supplier "Supplier B" --client "Client A" --amount 250

The transaction hash is generated per run, so the script can be run repeatedly.
A second deposit against a wallet that already has an open trade accumulates
onto it rather than opening a second one — which is B6, and worth testing
deliberately at least once.

This is a test utility. It writes a deposit row with a hash that is not on the
chain, so anything it creates should be cleaned out before go-live:

    DELETE FROM deposits WHERE tx_hash LIKE 'sim-%';
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiogram import Bot                                    # noqa: E402
from aiogram.client.default import DefaultBotProperties    # noqa: E402

from bot.notifier import Notifier                          # noqa: E402
from config import Config                                  # noqa: E402
from db.repo import Repo                                   # noqa: E402
from monitor.tron import DepositMonitor, TronClient        # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
log = logging.getLogger("simulate")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--supplier", default="Supplier A")
    p.add_argument("--client", default="Client A")
    p.add_argument("--amount", default="5000",
                   help="USDT, as a decimal string. Never a float.")
    p.add_argument("--hash", default=None,
                   help="Override the generated hash, e.g. to re-send one and "
                        "prove the duplicate is absorbed.")
    p.add_argument("--unconfirmed", action="store_true",
                   help="Record it as unconfirmed, to exercise the "
                        "15-minute unconfirmed-deposit alert.")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    amount = Decimal(args.amount)          # str, not float — see core/money.py

    config = Config.from_env()
    repo = await Repo.connect(config.database_url)

    default = DefaultBotProperties(parse_mode=None)
    bridge = Bot(config.bridge_bot_token, default=default)
    supplier = Bot(config.supplier_bot_token, default=default)
    client = Bot(config.client_bot_token, default=default)

    notifier = Notifier(bridge_bot=bridge, supplier_bot=supplier,
                        client_bot=client, repo=repo, config=config)
    tron = TronClient(
        tronscan_base=config.tronscan_api_base,
        trongrid_base=config.trongrid_api_base,
        trongrid_key=config.trongrid_api_key,
        usdt_contract=config.usdt_contract,
        asset=config.monitor_asset,
    )
    monitor = DepositMonitor(repo=repo, client=tron, notifier=notifier,
                             config=config)

    try:
        # Find the internal wallet for this pairing, exactly as the monitor
        # would: by wallet, never by asking anyone to choose (decision C3).
        wallets = await repo.list_wallets()
        match = [
            w for w in wallets
            if w["is_internal"]
            and w["supplier_label"] == args.supplier
            and w["client_label"] == args.client
        ]
        if not match:
            log.error("No internal wallet links %s to %s.", args.supplier, args.client)
            log.error("Registered internal wallets:")
            for w in wallets:
                if w["is_internal"]:
                    log.error("  %s -> %s  (%s)", w["supplier_label"],
                              w["client_label"], w["address"])
            return 1

        # monitored_wallets returns the shape _handle_deposit expects.
        wallet = next(
            w for w in await repo.monitored_wallets() if w["id"] == match[0]["id"]
        )

        tx_hash = args.hash or f"sim-{secrets.token_hex(16)}"
        transfer = {
            "tx_hash": tx_hash,
            "amount": amount,
            "from_address": "TSimulatedSupplierWalletDoNotUse1",
            "block": 0,
            "confirmed": not args.unconfirmed,
        }

        log.info("simulating %s USDT from %s to %s (wallet %s)",
                 amount, args.supplier, args.client, wallet["address"])
        log.info("hash %s", tx_hash)

        await monitor._handle_deposit(wallet, transfer)

        log.info("done — check the Bridge channel for the deposit notification")
        return 0
    finally:
        await tron.close()
        await repo.close()
        for b in (bridge, supplier, client):
            await b.session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
