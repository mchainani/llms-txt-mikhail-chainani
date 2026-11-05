"""Recover jobs abandoned by workers that stopped sending heartbeats."""

import logging

from llms_txt_gen.jobs import app

logger = logging.getLogger(__name__)


@app.periodic(cron="*/10 * * * *")
@app.task(name="retry_stalled_jobs", queueing_lock="retry_stalled_jobs")
async def retry_stalled_jobs(timestamp: int) -> None:
    """Requeue stalled jobs every ten minutes using Procrastinate's heartbeat check."""
    for job in await app.job_manager.get_stalled_jobs():
        await app.job_manager.retry_job(job)
        logger.info("Requeued stalled job %s (%s)", job.id, job.task_name)
