import asyncio
import logging
import os

from aiogram import Dispatcher
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo

from src.app.bot.compose import router as compose_router
from src.app.bot.handlers import router as commands_router
from src.app.bot.otp_mask import otp_mask_loop
from src.app.bot.sender import bot
from src.app.db import storage
from src.app.db.storage import init_db
from src.app.mail.blobstore import blob_maintenance_loop
from src.app.mail.lmtp import LMTPServer
from src.app.monitor import digest_loop, health_loop
from src.app.web.api import start_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    init_db(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
    await storage.db.ensure_indexes()
    logger.info("Database initialized")

    dp = Dispatcher()
    # Order matters: commands before FSM dialogs so /start etc. work in any state
    dp.include_router(commands_router)
    dp.include_router(compose_router)

    lmtp = LMTPServer(port=8024)
    background_tasks = []

    async def on_startup(dispatcher: Dispatcher):
        await bot.set_my_commands(
            [
                BotCommand(command="send", description="✉️ New email"),
                BotCommand(command="history", description="🗂 Email history"),
                BotCommand(command="unassigned", description="📥 Unassigned"),
                BotCommand(command="stats", description="📊 Statistics"),
                BotCommand(command="starred", description="⭐ Starred"),
                BotCommand(command="alias", description="🏷 New alias"),
                BotCommand(command="aliases", description="🏷 My aliases"),
                BotCommand(command="whereis", description="🔎 Where I signed up"),
                BotCommand(command="list_routes", description="📋 Routes"),
                BotCommand(command="cancel", description="❌ Cancel dialog"),
            ]
        )
        # The menu button opens the Mini App with initData (mail client); a keyboard "Compose" button does not provide initData
        webapp_url = os.getenv("WEBAPP_URL")
        if webapp_url:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="📬 Mail", web_app=WebAppInfo(url=webapp_url))
                )
            except Exception:
                logger.exception("Failed to set the menu button")
        logger.info("Starting LMTP Server...")
        await lmtp.start()
        await start_api(port=8080)
        background_tasks.append(asyncio.create_task(digest_loop()))
        background_tasks.append(asyncio.create_task(health_loop()))
        background_tasks.append(asyncio.create_task(otp_mask_loop()))
        background_tasks.append(asyncio.create_task(blob_maintenance_loop()))

    async def on_shutdown(dispatcher: Dispatcher):
        logger.info("Stopping LMTP Server...")
        for task in background_tasks:
            task.cancel()
        lmtp.stop()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Starting Bot Polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
