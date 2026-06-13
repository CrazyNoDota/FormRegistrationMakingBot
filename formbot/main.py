import asyncio
import logging
import signal
from telegram.ext import Application

import formbot.config as config
import formbot.memory as memory
import formbot.llm as llm
import formbot.queue_worker as qw
from formbot.bot import register_handlers


async def main() -> None:
    cfg = config.load()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    await memory.init(cfg.database_path)
    llm.init(cfg.nvidia_api_key)

    app = Application.builder().token(cfg.telegram_token).build()
    register_handlers(app)

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    async with app:
        await app.updater.start_polling(drop_pending_updates=True)
        await app.start()

        worker_task = asyncio.create_task(qw.worker())
        logger.info("Form bot started. Listening for messages...")

        await stop_event.wait()

        logger.info("Shutting down...")
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await app.updater.stop()
        await app.stop()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
