"""The Superset DB engine spec.

Superset is not a dependency of this package (it supplies itself at runtime),
so these tests run in two modes: if `superset` is importable they exercise the
class, otherwise they fall back to asserting the module's declared contract.
Task 9's in-container check is the real verification.
"""

import importlib.util
import pathlib

import pytest

SPEC_PATH = (
    pathlib.Path(__file__).parent.parent / "src" / "thunderduck_sqlalchemy" / "superset_spec.py"
)

superset_available = importlib.util.find_spec("superset") is not None


def test_the_module_exists():
    assert SPEC_PATH.is_file()


def test_it_declares_the_engine_name_and_cancel_hooks():
    """Read as source: the contract must hold whether or not Superset is here."""
    source = SPEC_PATH.read_text()
    assert 'engine = "thunderduck"' in source
    assert 'engine_name = "Thunderduck"' in source
    assert "def get_cancel_query_id" in source
    assert "def cancel_query" in source
    # DuckDB's spec injects connect_args["config"], which our keyword-only
    # connect() rejects -- without this override a thunderduck database cannot
    # be added to Superset at all.
    assert "def get_extra_params" in source
    # DuckDB's fetch_data tries to assign to cursor.description (which is
    # read-only here), failing SQL Lab execution after the query has already
    # run. Override to skip DuckDB's implementation.
    assert "def fetch_data" in source
    # Must NOT be overridden: True would make Superset's stop path report
    # success without ever calling cancel_query.
    assert "def has_implicit_cancel" not in source


def test_importing_it_without_superset_fails_loudly_not_silently():
    """It must not swallow the import error and register a broken spec.

    A guarded import here would let Superset load a spec whose base class is a
    stub, producing baffling behaviour instead of a clear startup failure.

    Checks every spelling of "swallow it", not just `except ImportError` --
    `ModuleNotFoundError` is a subclass, and `contextlib.suppress` hides the
    failure just as effectively, so a narrower assertion would be defeated by
    an edit that still violates the intent.
    """
    source = SPEC_PATH.read_text()
    assert "ImportError" not in source
    assert "ModuleNotFoundError" not in source
    assert "suppress" not in source
    # No try-block may precede the superset import at all.
    before_import = source.split("from superset")[0]
    assert "try:" not in before_import


@pytest.mark.skipif(not superset_available, reason="Superset is not installed")
def test_engine_spec_attributes():
    from thunderduck_sqlalchemy.superset_spec import ThunderduckEngineSpec

    assert ThunderduckEngineSpec.engine == "thunderduck"
    assert ThunderduckEngineSpec.engine_name == "Thunderduck"
    # Inherited, not overridden: True would make Superset's stop-query path
    # return success without ever calling cancel_query.
    assert ThunderduckEngineSpec.has_implicit_cancel() is False
    assert ThunderduckEngineSpec.supports_file_upload is False


@pytest.mark.skipif(not superset_available, reason="Superset is not installed")
def test_get_extra_params_does_not_inject_a_duckdb_config():
    """DuckDB's spec injects connect_args["config"], which our connect() rejects.

    Reproduced live: without this override, adding a thunderduck database in
    Superset fails outright with TypeError.
    """
    from unittest.mock import MagicMock

    from thunderduck_sqlalchemy.superset_spec import ThunderduckEngineSpec

    database = MagicMock()
    database.extra = "{}"
    extra = ThunderduckEngineSpec.get_extra_params(database)
    connect_args = extra.get("engine_params", {}).get("connect_args", {})
    assert "config" not in connect_args


@pytest.mark.skipif(not superset_available, reason="Superset is not installed")
def test_cancel_query_uses_the_cursor_execution_id():
    from unittest.mock import MagicMock

    from thunderduck_sqlalchemy.superset_spec import ThunderduckEngineSpec

    cursor = MagicMock()
    cursor.execution_id = "abc-123"
    assert ThunderduckEngineSpec.get_cancel_query_id(cursor, MagicMock()) == "abc-123"

    assert ThunderduckEngineSpec.cancel_query(cursor, MagicMock(), "abc-123") is True
    cursor.cancel.assert_called_once()


@pytest.mark.skipif(not superset_available, reason="Superset is not installed")
def test_fetch_data_does_not_assign_to_a_read_only_description():
    """DuckDB's fetch_data tries to assign cursor.description, which is read-only here.

    Reproduced live: without this override, SQL Lab execution fails with
    AttributeError after the query has already run as a Kubernetes Job,
    charging the customer for a query they never got results from.
    """
    from unittest.mock import MagicMock, PropertyMock

    from thunderduck_sqlalchemy.superset_spec import ThunderduckEngineSpec

    # Create a cursor with a read-only description property (like our real Cursor).
    cursor = MagicMock()
    # Mock fetchall to return some data
    cursor.fetchall.return_value = [[1, "a"], [2, "b"]]
    # Set description to a tuple (normal for DB cursors)
    type(cursor).description = PropertyMock(return_value=(("id",), ("name",)))

    # This should NOT raise AttributeError when trying to assign to description.
    # BaseEngineSpec.fetch_data does not touch description, only DuckDB's does.
    result = ThunderduckEngineSpec.fetch_data(cursor, limit=None)

    # Result should be the fetched data
    assert result == [[1, "a"], [2, "b"]]


@pytest.mark.skipif(not superset_available, reason="Superset is not installed")
def test_time_grains_are_inherited_from_duckdb():
    from thunderduck_sqlalchemy.superset_spec import ThunderduckEngineSpec

    grains = ThunderduckEngineSpec.get_time_grain_expressions()
    assert any("DATE_TRUNC" in str(v).upper() for v in grains.values())
