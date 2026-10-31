"""Create, inspect, and delete generation runs belonging to a request."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response

from llms_txt_gen.repositories import RunRepository
from llms_txt_gen.repositories.errors import NoDataFound
from llms_txt_gen.routers.dependencies import DatabaseConnection, ExistingRequest, RequestRun
from llms_txt_gen.routers.models import RunCreated, RunInfo

router = APIRouter(prefix="/llms-txt/requests/{request_id}/runs", tags=["runs"])


@router.post("", response_model=RunCreated, status_code=201)
async def create_run(request_id: ExistingRequest, connection: DatabaseConnection) -> RunCreated:
    try:
        run = await RunRepository(connection).create(request_id=request_id)
    except NoDataFound as error:
        raise HTTPException(status_code=404, detail="Request not found.") from error
    return RunCreated(id=run.id)


@router.get("", response_model=list[RunInfo])
async def list_runs(request_id: ExistingRequest, connection: DatabaseConnection) -> list[RunInfo]:
    rows = await RunRepository(connection).list_by_request_id(request_id)
    return [RunInfo.model_validate(row, from_attributes=True) for row in rows]


@router.get("/{run_id}", response_model=RunInfo)
async def get_run(run: RequestRun) -> RunInfo:
    return RunInfo.model_validate(run, from_attributes=True)


@router.delete("/{run_id}", status_code=204)
async def delete_run(
    request_id: ExistingRequest, run_id: UUID, connection: DatabaseConnection
) -> Response:
    repository = RunRepository(connection)
    try:
        run = await repository.read(run_id)
        if run.request_id != request_id:
            raise HTTPException(status_code=404, detail="Run not found.")
        await repository.delete(run_id)
    except NoDataFound as error:
        raise HTTPException(status_code=404, detail="Run not found.") from error
    return Response(status_code=204)
