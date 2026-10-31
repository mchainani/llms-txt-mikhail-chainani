"""Process requests by creating and enqueueing a new generation run."""

import logging
from dataclasses import replace
from uuid import UUID

from procrastinate import JobContext, RetryStrategy

from llms_txt_gen.database import database_connection
from llms_txt_gen.jobs import app
from llms_txt_gen.repositories import RequestRepository, RunRepository
from llms_txt_gen.repositories.entities import Request

logger = logging.getLogger(__name__)


@app.task(
    name="process_llms_txt_request",
    pass_context=True,
    retry=RetryStrategy(max_attempts=None, wait=30)
)
async def process_request(context: JobContext, request_id: str) -> None:
    request_uuid = UUID(request_id)
    async with database_connection() as connection:

        async def create_run_and_reschedule(current: Request) -> Request:
            if current.cron_expression is not None and current.job_id != str(context.job.id):
                logger.info("Skipping superseded request job %s", context.job.id)
                return current
            run = await RunRepository(connection).create(request_id=request_uuid)
            logger.info("Created run %s for request %s", run.id, request_uuid)
            if current.cron_expression is not None:
                return replace(current, job_id=None)
            return current

        await RequestRepository(connection).modify(request_uuid, create_run_and_reschedule)
