"""The PEP 249 exception hierarchy.

Tools inspect these relationships (SQLAlchemy maps DBAPI errors onto its own
hierarchy by isinstance checks), so the inheritance is part of the contract,
not an implementation detail.
"""

import pytest

from thunderduck_sqlalchemy import exceptions as exc


def test_error_is_an_exception():
    assert issubclass(exc.Error, Exception)


@pytest.mark.parametrize(
    "name",
    ["InterfaceError", "DatabaseError"],
)
def test_top_level_errors_inherit_error(name):
    assert issubclass(getattr(exc, name), exc.Error)


@pytest.mark.parametrize(
    "name",
    [
        "DataError",
        "OperationalError",
        "IntegrityError",
        "InternalError",
        "ProgrammingError",
        "NotSupportedError",
    ],
)
def test_database_error_subclasses(name):
    assert issubclass(getattr(exc, name), exc.DatabaseError)
