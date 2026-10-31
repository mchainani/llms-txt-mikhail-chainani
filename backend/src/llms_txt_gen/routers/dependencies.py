"""Shared database and request-scoped resource dependencies for HTTP routers."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, HTTPException
from psycopg import AsyncConnection

from llms_txt_gen.database import get_connection
from llms_txt_gen.repositories import RequestRepository, RunRepository
from llms_txt_gen.repositories.entities import Run
from llms_txt_gen.repositories.errors import NoDataFound

DatabaseConnection = Annotated[AsyncConnection, Depends(get_connection)]
RunSelector = UUID | Literal["latest"]


async def require_request(request_id: UUID, connection: DatabaseConnection) -> UUID:
    try:
        await RequestRepository(connection).read(request_id)
    except NoDataFound as error:
        raise HTTPException(status_code=404, detail="Request not found.") from error
    return request_id


ExistingRequest = Annotated[UUID, Depends(require_request)]


async def resolve_run(run_id: RunSelector, request_id: UUID, connection: DatabaseConnection) -> Run:
    await require_request(request_id, connection)
    repository = RunRepository(connection)
    try:
        if run_id == "latest":
            return await repository.get_latest_by_request_id(request_id)
        row = await repository.read(run_id)
    except NoDataFound as error:
        raise HTTPException(status_code=404, detail="Run not found.") from error
    if row.request_id != request_id:
        raise HTTPException(status_code=404, detail="Run not found.")
    return row


RequestRun = Annotated[Run, Depends(resolve_run)]
