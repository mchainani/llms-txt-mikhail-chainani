"""Inspect the generated file belonging to a request's run."""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from llms_txt_gen.repositories import FileRepository
from llms_txt_gen.repositories.errors import NoDataFound
from llms_txt_gen.routers.dependencies import DatabaseConnection, resolve_run
from llms_txt_gen.routers.models import FileInfo

router = APIRouter(prefix="/llms-txt/requests/{request_id}/runs/{run_id}/files", tags=["files"])


@router.get("", response_model=FileInfo)
async def get_file(request_id: UUID, run_id: UUID, connection: DatabaseConnection) -> FileInfo:
    run = await resolve_run(run_id, request_id, connection)
    if run.file_id is None:
        raise HTTPException(status_code=404, detail="File not found.")
    try:
        row = await FileRepository(connection).read(run.file_id)
    except NoDataFound as error:
        raise HTTPException(status_code=404, detail="File not found.") from error
    return FileInfo.model_validate(row, from_attributes=True)
