"""Create, inspect, and delete persisted llms.txt requests."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response

from llms_txt_gen.repositories import RequestRepository
from llms_txt_gen.repositories.errors import NoDataFound
from llms_txt_gen.routers.dependencies import DatabaseConnection
from llms_txt_gen.routers.models import FetchPageRequest, RequestInfo

router = APIRouter(prefix="/llms-txt/requests", tags=["requests"])


@router.post("", response_model=RequestInfo, status_code=201)
async def create_request(payload: FetchPageRequest, connection: DatabaseConnection) -> RequestInfo:
    row = await RequestRepository(connection).create(
        url=str(payload.url), cron_expression=payload.cron_expression
    )
    return RequestInfo.model_validate(row, from_attributes=True)


@router.get("", response_model=list[RequestInfo])
async def list_requests(connection: DatabaseConnection) -> list[RequestInfo]:
    rows = await RequestRepository(connection).list()
    return [RequestInfo.model_validate(row, from_attributes=True) for row in rows]


@router.get("/{request_id}", response_model=RequestInfo)
async def get_request(request_id: UUID, connection: DatabaseConnection) -> RequestInfo:
    try:
        row = await RequestRepository(connection).read(request_id)
    except NoDataFound as error:
        raise HTTPException(status_code=404, detail="Request not found.") from error
    return RequestInfo.model_validate(row, from_attributes=True)


@router.delete("/{request_id}", status_code=204)
async def delete_request(request_id: UUID, connection: DatabaseConnection) -> Response:
    try:
        await RequestRepository(connection).delete(request_id)
    except NoDataFound as error:
        raise HTTPException(status_code=404, detail="Request not found.") from error
    return Response(status_code=204)
