"""Shared SQL operations and transaction/error boundaries for entity repositories."""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from inspect import isawaitable
from typing import Any, ClassVar
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from llms_txt_gen.repositories.errors import NoDataFound, translate_database_error


class Repository[T]:
    """Use the caller's connection; nested operations use transaction savepoints.

    Callers group multiple repository operations in connection.transaction() when
    they must commit together. A modify callback must use this same connection
    for related writes and should avoid slow external work while holding its lock.
    """

    table: ClassVar[str]
    columns: ClassVar[tuple[str, ...]]
    mutable_columns: ClassVar[tuple[str, ...]]
    order_by: ClassVar[tuple[str, ...]] = ("id",)
    entity_type: type[T]

    def __init__(self, connection: psycopg.AsyncConnection) -> None:
        self.connection = connection

    @property
    def _columns_sql(self) -> sql.Composed:
        return sql.SQL(", ").join(map(sql.Identifier, self.columns))

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[None]:
        try:
            async with self.connection.transaction():
                yield
        except psycopg.Error as error:
            # Translate only after rollback, so an enclosing transaction can continue.
            raise translate_database_error(error) from error

    async def _fetch_one(self, query: sql.Composable, params: tuple = ()) -> T:
        async with self.connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(query, params)
            row = await cursor.fetchone()
        if row is None:
            raise NoDataFound(self.entity_type.__name__, params[-1])
        return self.entity_type(**row)

    async def _create(self, values: Mapping[str, Any]) -> T:
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
            sql.Identifier(self.table),
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
            self._columns_sql,
        )
        async with self._operation():
            return await self._fetch_one(query, tuple(values.values()))

    async def _read(self, entity_id: UUID, *, for_update: bool = False) -> T:
        query = sql.SQL("SELECT {} FROM {} WHERE id = %s{}").format(
            self._columns_sql,
            sql.Identifier(self.table),
            sql.SQL(" FOR UPDATE" if for_update else ""),
        )
        return await self._fetch_one(query, (entity_id,))

    async def read(self, entity_id: UUID) -> T:
        """Read an entity by ID or raise NoDataFound."""
        async with self._operation():
            return await self._read(entity_id)

    async def list(self) -> list[T]:
        """Return all entities in deterministic descending order."""
        query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
            self._columns_sql,
            sql.Identifier(self.table),
            sql.SQL(", ").join(
                sql.SQL("{} DESC").format(sql.Identifier(column)) for column in self.order_by
            ),
        )
        async with self._operation():
            async with self.connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(query)
                rows = await cursor.fetchall()
            return [self.entity_type(**row) for row in rows]

    async def delete(self, entity_id: UUID) -> None:
        """Delete an entity by ID or raise NoDataFound."""
        query = sql.SQL("DELETE FROM {} WHERE id = %s RETURNING {}").format(
            sql.Identifier(self.table), self._columns_sql
        )
        async with self._operation():
            await self._fetch_one(query, (entity_id,))

    async def modify(self, entity_id: UUID, modifier: Callable[[T], T | Awaitable[T]]) -> T:
        """Lock, read, transform, and update in one transaction.

        The callback receives the current immutable entity and returns its replacement.
        Sync and async callbacks are supported. Returning the unchanged entity is a
        no-op. IDs and database-generated timestamps cannot be changed by callbacks.
        """
        if not self.mutable_columns:
            raise ValueError(f"{self.entity_type.__name__} cannot be modified")
        async with self._operation():
            current = await self._read(entity_id, for_update=True)
            modified = modifier(current)
            if isawaitable(modified):
                modified = await modified
            self._validate_modification(current, modified)
            if modified == current:
                return current
            assignments = [
                sql.SQL("{} = %s").format(sql.Identifier(column)) for column in self.mutable_columns
            ]
            if "timestamp" in self.columns:
                assignments.append(sql.SQL('"timestamp" = now()'))
            query = sql.SQL("UPDATE {} SET {} WHERE id = %s RETURNING {}").format(
                sql.Identifier(self.table), sql.SQL(", ").join(assignments), self._columns_sql
            )
            values = tuple(getattr(modified, column) for column in self.mutable_columns)
            return await self._fetch_one(query, (*values, entity_id))

    def _validate_modification(self, current: T, modified: T) -> None:
        if not isinstance(modified, self.entity_type):
            raise TypeError(f"Modifier must return {self.entity_type.__name__}")
        for column in self.columns:
            if column not in self.mutable_columns and (
                getattr(modified, column) != getattr(current, column)
            ):
                raise ValueError(f"Cannot modify {column}")
