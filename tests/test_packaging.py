"""The entry points must actually resolve.

If these break, the package installs cleanly and then does nothing: neither
`create_engine("thunderduck://…")` nor Superset's database picker would find
it. Worth a test.
"""

from importlib.metadata import entry_points


def test_sqlalchemy_dialect_entry_point_is_registered():
    eps = {ep.name: ep for ep in entry_points(group="sqlalchemy.dialects")}
    assert "thunderduck" in eps
    assert eps["thunderduck"].load().__name__ == "ThunderduckDialect"


def test_qualified_dialect_entry_point_is_registered():
    eps = {ep.name: ep for ep in entry_points(group="sqlalchemy.dialects")}
    assert "thunderduck.rest" in eps


def test_superset_engine_spec_entry_point_is_declared():
    """Declared here; loading it needs Superset, which Task 9 verifies."""
    eps = {ep.name: ep for ep in entry_points(group="superset.db_engine_specs")}
    assert "thunderduck" in eps
    assert eps["thunderduck"].value.endswith("superset_spec:ThunderduckEngineSpec")


def test_engine_can_be_created_from_a_url():
    from sqlalchemy import create_engine

    engine = create_engine("thunderduck://:tdk_x@api.example.test/")
    assert engine.dialect.name == "thunderduck"


def test_reported_version_matches_distribution_metadata():
    """`__version__` must not drift from what `pip` and PyPI call the release."""
    from importlib.metadata import version

    from thunderduck_sqlalchemy import __version__

    assert __version__ == version("thunderduck-sqlalchemy")
