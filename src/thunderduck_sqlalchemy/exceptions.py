"""PEP 249 exception hierarchy.

Defined here rather than in `dbapi.py` so every module (client, types,
dialect) can raise the right class without importing the DBAPI layer, which
would be a circular import.
"""


class Error(Exception):
    """Base class for every error this driver raises."""


class InterfaceError(Error):
    """A problem with the driver or the connection itself, not the database.

    Used for bad connection URLs, missing tokens and auth failures.
    """


class DatabaseError(Error):
    """A problem reported by thunderduck about the request."""


class DataError(DatabaseError):
    """A value could not be represented in SQL (e.g. NaN, a NUL byte)."""


class OperationalError(DatabaseError):
    """A run-time failure outside the caller's control.

    Cancelled queries, poll timeouts and expired result sets.
    """


class IntegrityError(DatabaseError):
    """Relational integrity was violated. Not raised today (read-only)."""


class InternalError(DatabaseError):
    """The driver reached a state that should be impossible."""


class ProgrammingError(DatabaseError):
    """The caller did something wrong: bad SQL, or an unbindable parameter."""


class NotSupportedError(DatabaseError):
    """A DBAPI feature thunderduck does not have (transactions, DML)."""
