import asyncio
import logging
from formbot.agent import FormJob, analyze_and_run, submit_from_session

logger = logging.getLogger(__name__)

task_queue: asyncio.Queue = asyncio.Queue(maxsize=20)


async def worker() -> None:
    while True:
        job: FormJob = await task_queue.get()
        try:
            if job.action == "analyze":
                await analyze_and_run(job)
            elif job.action == "submit":
                await submit_from_session(job)
        except Exception as e:
            logger.exception("Worker unhandled error")
            try:
                await job.bot.send_message(job.chat_id, f"❌ Непредвиденная ошибка: {e}")
            except Exception:
                pass
        finally:
            task_queue.task_done()


def enqueue(job: FormJob) -> bool:
    try:
        task_queue.put_nowait(job)
        return True
    except asyncio.QueueFull:
        return False
