"""Persistence errors exposed independently of the Postgres driver."""

from uuid import UUID

import psycopg


class RepositoryError(Exception):
    """Base error for repository operations."""


class NoDataFound(RepositoryError):
    def __init__(self, entity: str, entity_id: UUID) -> None:
        self.entity = entity
        self.entity_id = entity_id
        super().__init__(f"{entity} {entity_id} was not found")


class DatabaseError(RepositoryError):
    def __init__(self, message: str, *, constraint_name: str | None = None) -> None:
        self.constraint_name = constraint_name
        super().__init__(message)


class ConstraintViolation(DatabaseError):
    """A write violated a database integrity constraint."""


class UniqueConstraintViolation(ConstraintViolation):
    """A primary key or unique constraint already exists."""


class ForeignKeyViolation(ConstraintViolation):
    """A referenced row is missing or still in use."""


class NotNullViolation(ConstraintViolation):
    """A required column received NULL."""


class CheckConstraintViolation(ConstraintViolation):
    """A value failed a database check constraint."""


class SerializationFailure(DatabaseError):
    """The transaction must be retried because of a concurrent write."""


class DeadlockDetected(DatabaseError):
    """The transaction was aborted to resolve a deadlock."""


class DatabaseUnavailable(DatabaseError):
    """The database connection failed."""


def translate_database_error(error: psycopg.Error) -> DatabaseError:
    error_type = {
        "23505": UniqueConstraintViolation,
        "23503": ForeignKeyViolation,
        "23502": NotNullViolation,
        "23514": CheckConstraintViolation,
        "40001": SerializationFailure,
        "40P01": DeadlockDetected,
    }.get(error.sqlstate)
    if error_type is None:
        if isinstance(error, psycopg.IntegrityError):
            error_type = ConstraintViolation
        elif isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError)):
            error_type = DatabaseUnavailable
        else:
            error_type = DatabaseError
    return error_type(str(error), constraint_name=error.diag.constraint_name)
