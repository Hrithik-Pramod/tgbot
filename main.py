"""
Entry point.

Runs three bots and the deposit monitor in one process, sharing one database
pool. Three tokens (decision D3) so Telegram shows each party only its own
command menu and a leaked supplier token cannot reach the Bridge.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from bot import bridge_bot, bridge_trade, client_bot, membership, supplier_bot
from bot.auth import ChatRoleMiddleware
from bot.notifier import Notifier
from config import Config
from db.repo import Repo
from monitor.tron import DepositMonitor, TronClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("main")


def build_dispatcher(routers, repo, notifier, role: str, config) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    # Authorisation is by chat, not by individual user (client decision,
    # 7 Sep 2026). See bot/auth.py.
    middleware = ChatRoleMiddleware(
        repo, role,
        bridge_user_id=config.bridge_user_id,
        strict_bridge_user=config.strict_bridge_user,
    )
    dp.message.middleware(middleware)
    dp.callback_query.middleware(middleware)

    # Handlers receive these by name rather than reaching for globals.
    dp["repo"] = repo
    dp["notifier"] = notifier

    for router in routers:
        dp.include_router(router)
    return dp


async def main() -> None:
    config = Config.from_env()
    repo = await Repo.connect(config.database_url)

    default = DefaultBotProperties(parse_mode=None)
    bridge = Bot(config.bridge_bot_token, default=default)
    supplier = Bot(config.supplier_bot_token, default=default)
    client = Bot(config.client_bot_token, default=default)

    notifier = Notifier(
        bridge_bot=bridge, supplier_bot=supplier, client_bot=client,
        repo=repo, config=config,
    )

    tron = TronClient(
        tronscan_base=config.tronscan_api_base,
        trongrid_base=config.trongrid_api_base,
        trongrid_key=config.trongrid_api_key,
        usdt_contract=config.usdt_contract,
    )
    monitor = DepositMonitor(
        repo=repo, client=tron, notifier=notifier, config=config
    )

    # membership.router is added to the supplier and client bots only - those
    # are the groups whose membership grants command access.
    dispatchers = [
        (build_dispatcher([bridge_bot.router, bridge_trade.router],
                          repo, notifier, "bridge", config), bridge),
        (build_dispatcher([membership.router, supplier_bot.router],
                          repo, notifier, "supplier", config), supplier),
        (build_dispatcher([membership.router, client_bot.router],
                          repo, notifier, "client", config), client),
    ]

    log.info("starting three bots and the deposit monitor")
    try:
        await asyncio.gather(
            *[dp.start_polling(b, handle_signals=False) for dp, b in dispatchers],
            monitor.run_forever(),
        )
    finally:
        await tron.close()
        await repo.close()
        for _, b in dispatchers:
            await b.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
