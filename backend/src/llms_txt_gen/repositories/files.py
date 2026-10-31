"""Persistence for generated llms.txt documents."""

from uuid import UUID, uuid4

from llms_txt_gen.repositories.base import Repository
from llms_txt_gen.repositories.entities import File


class FileRepository(Repository[File]):
    table = "llms_txt_files"
    columns = ("id", "type", "content", "created_at", "timestamp")
    mutable_columns = ()
    entity_type = File

    async def create(self, *, type: str, content: str, id: UUID | None = None) -> File:
        return await self._create(
            {"id": id if id is not None else uuid4(), "type": type, "content": content}
        )
