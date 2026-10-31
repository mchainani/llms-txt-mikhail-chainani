"""Shared Procrastinate application for request and run workers."""

import os

from dotenv import load_dotenv
from procrastinate import App, PsycopgConnector
from psycopg import AsyncConnection

load_dotenv()

app = App(
    connector=PsycopgConnector(conninfo=os.environ["DATABASE_URL"]),
    import_paths=[
        "llms_txt_gen.jobs.run_worker",
        "llms_txt_gen.jobs.request_worker",
    ],
)


async def cancel_job(connection: AsyncConnection, job_id: str) -> None:
    """Cancel a queued job or request abortion using the caller's transaction."""
    await app.job_manager.cancel_job_by_id_async(int(job_id), abort=True, connection=connection)
