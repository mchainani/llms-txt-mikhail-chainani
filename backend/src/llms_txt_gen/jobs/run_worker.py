"""Create generation runs and process their queued background jobs."""

import logging
from dataclasses import replace
from uuid import UUID

from procrastinate import JobContext, RetryStrategy

from llms_txt_gen.database import database_connection
from llms_txt_gen.jobs import app
from llms_txt_gen.repositories import FileRepository, RequestRepository, RunRepository
from llms_txt_gen.repositories.entities import Run
from llms_txt_gen.repositories.errors import NoDataFound

MAX_RETRY_COUNT = 5

logger = logging.getLogger(__name__)


@app.task(
    name="process_llms_txt_run",
    pass_context=True,
    retry=RetryStrategy(max_attempts=None, wait=30),
)
async def process_run(context: JobContext, run_id: str) -> None:
    run_uuid = UUID(run_id)
    async with database_connection() as connection:

        def start_attempt(current: Run) -> Run:
            if current.status != "pending":
                return current
            retry_count = current.retry_count + 1
            return replace(
                current,
                retry_count=retry_count,
                status="failed" if retry_count > MAX_RETRY_COUNT else "pending",
            )

        try:
            record = await RunRepository(connection).modify(run_uuid, start_attempt)
        except NoDataFound:
            logger.info("Skipping deleted run %s (job_id=%s)", run_id, context.job.id)
            return
        logger.info(
            "Processing run %s (job_id=%s, retry_count=%s)",
            run_id,
            context.job.id,
            record.retry_count,
        )
        if record.status != "pending":
            logger.info("Skipping run %s with status %s", run_id, record.status)
            return
        request = await RequestRepository(connection).read(record.request_id)

    # No database connection or transaction is held while crawling.
    # The server imports this task to enqueue jobs without loading Crawlee.
    from llms_txt_gen.crawling.fetcher import fetch_page
    from llms_txt_gen.generation.generator import build_document, render_document

    crawl_results = await fetch_page(request.url, request.configs)
    document = build_document(crawl_results)
    llms_txt = render_document(document)

    async with database_connection() as connection:
        file_id: UUID | None = None

        async def complete(locked_run: Run) -> Run:
            nonlocal file_id
            if locked_run.status != "pending":
                logger.info("Skipping run %s with status %s", run_id, locked_run.status)
                return locked_run
            if locked_run.request_id != record.request_id:
                raise ValueError(
                    "Run request changed during generation; retry with its current URL"
                )
            file = await FileRepository(connection).create(type="text/markdown", content=llms_txt)
            file_id = file.id
            return replace(
                locked_run, file_id=file.id, status="success", num_links=len(crawl_results.results)
            )

        await RunRepository(connection).modify(run_uuid, complete)
        if file_id is None:
            return
    logger.info("Generated markdown for run %s:\n%s", run_id, llms_txt)
