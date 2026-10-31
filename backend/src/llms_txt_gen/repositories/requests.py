"""Persistence for llms.txt generation requests."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from inspect import isawaitable
from uuid import UUID, uuid4

from llms_txt_gen.cron import next_cron_time
from llms_txt_gen.repositories.base import Repository
from llms_txt_gen.repositories.entities import Request
from llms_txt_gen.repositories.runs import RunRepository


class RequestRepository(Repository[Request]):
    table = "llms_txt_requests"
    columns = ("id", "url", "created_at", "timestamp", "job_id", "cron_expression", "configs")
    mutable_columns = ("job_id",)
    order_by = ("created_at", "id")
    entity_type = Request

    async def delete(self, entity_id: UUID) -> None:
        """Stop processing and delete the request, its runs, and their files atomically."""
        from llms_txt_gen.jobs import cancel_job

        async with self._operation():
            request = await self._read(entity_id, for_update=True)
            if request.job_id is not None:
                await cancel_job(self.connection, request.job_id)
            runs = RunRepository(self.connection)
            for run in await runs.list_by_request_id(entity_id):
                await runs.delete(run.id)
            await super().delete(entity_id)

    async def create(
        self,
        *,
        url: str,
        job_id: str | None = None,
        cron_expression: str | None = None,
        configs: str | None = None,
        id: UUID | None = None,
    ) -> Request:
        """Create a request and its missing processing job at the next cron time or now."""
        schedule_at = next_cron_time(cron_expression) if cron_expression is not None else None
        request_id = id if id is not None else uuid4()
        async with self._operation():
            if job_id is None:
                job_id = await self._enqueue(request_id, schedule_at=schedule_at)
            return await self._create(
                {
                    "id": request_id,
                    "url": url,
                    "job_id": job_id,
                    "cron_expression": cron_expression,
                    "configs": configs,
                }
            )

    async def modify(
        self, entity_id: UUID, modifier: Callable[[Request], Request | Awaitable[Request]]
    ) -> Request:
        """Apply a locked update, scheduling a cron job when its job ID is cleared."""

        async def modify_and_schedule(current: Request) -> Request:
            modified = modifier(current)
            if isawaitable(modified):
                modified = await modified
            self._validate_modification(current, modified)
            if modified.job_id is None and modified.cron_expression is not None:
                job_id = await self._enqueue(
                    modified.id, schedule_at=next_cron_time(modified.cron_expression)
                )
                return replace(modified, job_id=job_id)
            return modified

        return await super().modify(entity_id, modify_and_schedule)

    async def _enqueue(self, request_id: UUID, *, schedule_at: datetime | None = None) -> str:
        # Import lazily because the worker also uses RequestRepository.
        from llms_txt_gen.jobs.request_worker import process_request

        scheduled_job_id = await process_request.configure(
            connection=self.connection,
            lock=f"llms-txt-request:{request_id}",
            schedule_at=schedule_at,
        ).defer_async(request_id=str(request_id))
        return str(scheduled_job_id)
