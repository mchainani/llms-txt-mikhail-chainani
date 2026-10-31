"""Consistent lifecycle logs for background job invocations."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from procrastinate import JobContext


@contextmanager
def log_job(logger: logging.Logger, context: JobContext, **arguments: str) -> Iterator[None]:
    job = context.job
    fields = {
        "job_type": job.task_name,
        "job_id": job.id,
        **arguments,
        "queue": job.queue,
        "attempts": job.attempts,
        "worker_id": job.worker_id,
    }
    details = ", ".join(f"{key}={value}" for key, value in fields.items())
    started = perf_counter()
    logger.info("Job started (%s)", details, extra=fields)
    try:
        yield
    except Exception:
        runtime = perf_counter() - started
        logger.exception(
            "Job failed (%s, runtime_seconds=%.3f)",
            details,
            runtime,
            extra={**fields, "runtime_seconds": runtime},
        )
        raise
    else:
        runtime = perf_counter() - started
        logger.info(
            "Job succeeded (%s, runtime_seconds=%.3f)",
            details,
            runtime,
            extra={**fields, "runtime_seconds": runtime},
        )
