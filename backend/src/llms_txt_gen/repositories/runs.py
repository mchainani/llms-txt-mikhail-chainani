"""Persistence for llms.txt generation runs."""

from uuid import UUID, uuid4

from psycopg import sql
from psycopg.rows import dict_row

from llms_txt_gen.repositories.base import Repository
from llms_txt_gen.repositories.entities import Run
from llms_txt_gen.repositories.files import FileRepository


class RunRepository(Repository[Run]):
    table = "llms_txt_runs"
    columns = (
        "id",
        "request_id",
        "created_at",
        "timestamp",
        "status",
        "file_id",
        "job_id",
        "num_links",
        "retry_count",
    )
    mutable_columns = ("status", "file_id", "num_links", "retry_count")
    order_by = ("created_at", "id")
    entity_type = Run

    async def delete(self, entity_id: UUID) -> None:
        """Stop processing and delete the run and its generated file atomically."""
        from llms_txt_gen.jobs import cancel_job

        async with self._operation():
            run = await self._read(entity_id, for_update=True)
            if run.job_id is not None:
                await cancel_job(self.connection, run.job_id)
            if run.file_id is not None:
                await FileRepository(self.connection).delete(run.file_id)
            await super().delete(entity_id)

    async def create(
        self,
        *,
        request_id: UUID,
        status: str = "pending",
        file_id: UUID | None = None,
        num_links: int | None = None,
        retry_count: int = 0,
        id: UUID | None = None,
    ) -> Run:
        """Enqueue processing and create its run atomically on this connection."""
        # Import lazily because the worker also uses RunRepository.
        from llms_txt_gen.jobs.run_worker import process_run
        from llms_txt_gen.repositories.requests import RequestRepository

        run_id = id if id is not None else uuid4()
        async with self._operation():
            # Serialize with request deletion before creating a child or its job.
            await RequestRepository(self.connection)._read(request_id, for_update=True)
            job_id = await process_run.configure(
                connection=self.connection, lock=f"llms-txt-run:{run_id}"
            ).defer_async(run_id=str(run_id))
            return await self._create(
                {
                    "id": run_id,
                    "request_id": request_id,
                    "status": status,
                    "file_id": file_id,
                    "job_id": str(job_id),
                    "num_links": num_links,
                    "retry_count": retry_count,
                }
            )

    async def list_by_request_id(self, request_id: UUID, *, limit: int | None = None) -> list[Run]:
        """Return a request's runs in the request/created-at index order."""
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        query = sql.SQL("SELECT {} FROM {} WHERE request_id = %s ORDER BY created_at DESC").format(
            self._columns_sql, sql.Identifier(self.table)
        )
        params = (request_id,)
        if limit is not None:
            query += sql.SQL(" LIMIT %s")
            params = (request_id, limit)
        async with self._operation():
            async with self.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(query, params)
                rows = await cursor.fetchall()
            return [self.entity_type(**row) for row in rows]

    async def get_latest_by_request_id(self, request_id: UUID) -> Run:
        """Return the newest run for a request or raise NoDataFound."""
        query = sql.SQL(
            "SELECT {} FROM {} WHERE request_id = %s ORDER BY created_at DESC LIMIT 1"
        ).format(self._columns_sql, sql.Identifier(self.table))
        async with self._operation():
            return await self._fetch_one(query, (request_id,))
