from __future__ import annotations
from typing import Optional, Callable
from telegram.ext import Application


JOB_PREFIX = "check:"


class BotScheduler:
    def __init__(self, app: Application):
        self.app = app

    def _job_name(self, user_id: int) -> str:
        return f"{JOB_PREFIX}{user_id}"

    async def upsert_user_job(
        self,
        user_id: int,
        seconds: int,
        callback: Callable,
        data: Optional[dict] = None,
    ):
        # удаляем старую
        name = self._job_name(user_id)
        for job in self.app.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
        # создаём новую периодическую
        self.app.job_queue.run_repeating(
            callback=callback,
            interval=seconds,
            data=data or {"user_id": user_id},
            name=name,
            chat_id=user_id,  # чтобы у callback был chat_id
            first=seconds,  # первый запуск через заданный интервал
        )

    def remove_user_job(self, user_id: int):
        name = self._job_name(user_id)
        for job in self.app.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
