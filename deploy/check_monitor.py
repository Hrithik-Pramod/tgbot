"""
Pre-flight check for the deposit monitor.

WHY THIS EXISTS
---------------
A wallet that has never received anything and a monitor that cannot reach the
internet produce identical evidence: no cursor, no deposits, no errors, nothing
in the log. The poller only records a cursor when it finds transfers, so
silence proves nothing either way.

This asks the same question the monitor asks, from the same machine, through
the same client and the same asset setting — and prints the answer. Run it
before asking anyone to send real funds.

It reads only. It writes nothing to the database and notifies no one.

USAGE

    # Check every monitored wallet, in whatever asset the bot is configured for
    docker compose exec bot python deploy/check_monitor.py

    # Also prove the network path end to end against a known-busy address.
    # Nothing is recorded — this address is not in the database.
    docker compose exec bot python deploy/check_monitor.py --probe

    # Check one specific address before adding it
    docker compose exec bot python deploy/check_monitor.py \
        --address TXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Config                       # noqa: E402
from db.repo import Repo                        # noqa: E402
from monitor.tron import TronClient             # noqa: E402

# A high-traffic exchange wallet. Used only to prove the request path works:
# if this returns transfers and your own wallet returns none, the monitor is
# healthy and your wallet is simply quiet. If BOTH return none, the problem is
# the server, the API, or the asset setting — not the wallet.
PROBE_ADDRESS = "TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9"


async def report(client: TronClient, label: str, address: str) -> bool:
    print(f"\n{label}")
    print(f"  {address}")
    try:
        transfers = await client.incoming_transfers(address, None)
    except Exception as exc:
        print(f"  FAILED — both providers refused: {exc}")
        return False

    if not transfers:
        print("  reachable, no incoming transfers found")
        return True

    print(f"  reachable, {len(transfers)} incoming transfer(s), newest last:")
    for t in transfers[-3:]:
        flag = "confirmed" if t["confirmed"] else "unconfirmed"
        print(f"    {t['amount']:>16}  {flag:<12} {t['tx_hash']}")
    return True


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--address", help="Check one address instead of the database's")
    p.add_argument("--probe", action="store_true",
                   help="Also query a known-busy address to prove the path works")
    args = p.parse_args()

    config = Config.from_env()
    client = TronClient(
        tronscan_base=config.tronscan_api_base,
        trongrid_base=config.trongrid_api_base,
        trongrid_key=config.trongrid_api_key,
        usdt_contract=config.usdt_contract,
        asset=config.monitor_asset,
    )

    print(f"MONITOR_ASSET = {client.asset}"
          + ("   <-- TEST SETTING, not for production" if client.asset == "TRX" else ""))

    ok = True
    try:
        if args.address:
            ok &= await report(client, "Requested address", args.address)
        else:
            repo = await Repo.connect(config.database_url)
            try:
                wallets = await repo.monitored_wallets()
            finally:
                await repo.close()

            if not wallets:
                print("\nNo wallets are marked as monitored. Nothing would be "
                      "detected even if funds arrived.")
                ok = False
            for w in wallets:
                cursor = w["last_timestamp_ms"]
                ok &= await report(
                    client,
                    f"Wallet {w['id']}"
                    + (f"  (cursor {cursor})" if cursor else "  (no cursor yet)"),
                    w["address"],
                )

        if args.probe:
            ok &= await report(client, "Probe — known-busy address, not recorded",
                               PROBE_ADDRESS)
    finally:
        await client.close()

    print("\n" + ("All queries answered. The monitor can see the chain."
                  if ok else
                  "Something did not answer. Do not ask anyone to send funds yet."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
