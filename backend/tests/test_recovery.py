"""Stalled jobs are returned to the queue by the recovery task."""

import anyio

from llms_txt_gen.jobs import app


def test_recovery_task_is_registered():
    app.perform_import_paths()
    assert "retry_stalled_jobs" in app.tasks


def test_recovery_requeues_orphaned_job(run_database):
    from llms_txt_gen.jobs.recovery import retry_stalled_jobs

    job_id = run_database.execute(
        "INSERT INTO procrastinate_jobs (task_name, queue_name, args, status) "
        "VALUES ('process_llms_txt_run', 'default', '{}', 'doing') RETURNING id"
    ).fetchone()["id"]

    async def recover():
        import os

        from procrastinate import PsycopgConnector

        with app.replace_connector(PsycopgConnector(conninfo=os.environ["DATABASE_URL"])):
            async with app.open_async():
                await retry_stalled_jobs(timestamp=0)

    anyio.run(recover)
    assert run_database.execute(
        "SELECT status FROM procrastinate_jobs WHERE id = %s", (job_id,)
    ).fetchone()["status"] == "todo"
