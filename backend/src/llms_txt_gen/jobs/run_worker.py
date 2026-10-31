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
    logger.info(
        "Processing run %s (job_id=%s, attempt=%s)",
        run_id,
        context.job.id,
        context.job.attempts + 1,
    )
    async with database_connection() as connection:
        if context.job.attempts > MAX_RETRY_COUNT:

            def fail_if_pending(current: Run) -> Run:
                if current.status == "pending":
                    return replace(current, status="failed")
                return current

            try:
                await RunRepository(connection).modify(run_uuid, fail_if_pending)
            except NoDataFound:
                # A deleted run has nothing left to mark failed at the retry cutoff.
                pass
            logger.warning("Run %s exceeded retry limit (%s)", run_id, context.job.attempts)
            return
        record = await RunRepository(connection).read(run_uuid)
        if record.status != "pending":
            logger.info("Skipping run %s with status %s", run_id, record.status)
            return
        request = await RequestRepository(connection).read(record.request_id)

    # No database connection or transaction is held while crawling.
    # The server imports this task to enqueue jobs without loading Crawlee.
    from llms_txt_gen.crawling.fetcher import fetch_page
    from llms_txt_gen.generation.generator import build_document, render_document

    crawl_results = await fetch_page(request.url)
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
