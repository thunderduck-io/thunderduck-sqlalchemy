# thunderduck-sqlalchemy

A SQLAlchemy dialect and PEP 249 DBAPI for [thunderduck](https://thunderduck.ai).
Query your lakehouse from anything that speaks SQLAlchemy — Superset, Airflow,
Dagster, Redash, pandas, Jupyter, Streamlit.

```bash
pip install thunderduck-sqlalchemy
```

Works with SQLAlchemy 1.4 and 2.x, on Python 3.10+ — so it installs cleanly
into Apache Superset (which pins SQLAlchemy 1.4) as well as modern Airflow,
Dagster and pandas environments.

[Source on GitHub](https://github.com/thunderduck-io/thunderduck-sqlalchemy) ·
[Documentation](https://docs.thunderduck.ai/clients/sqlalchemy) ·
[PyPI](https://pypi.org/project/thunderduck-sqlalchemy/)

## Connect

Create an API token in the thunderduck console (**Settings → API Tokens**; it
starts with `tdk_` and is shown once), then:

```python
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

engine = create_engine(
    "thunderduck://:tdk_your_token@api.thunderduck.ai/",
    poolclass=NullPool,
)

with engine.connect() as conn:
    for row in conn.execute(
        text('SELECT * FROM "tndr_postgres_demo"."ecommerce"."orders" LIMIT 10')
    ):
        print(row)
```

pandas works directly:

```python
import pandas as pd

df = pd.read_sql('SELECT * FROM "thunder_duck_demo"."iris"', engine)
```

### URL options

| Option | Default | Meaning |
| --- | --- | --- |
| password / `?token=` | — | Required. Your `tdk_…` API token. |
| `?ssl=false` | `true` | Use plain HTTP instead of HTTPS. Only for a server that does not terminate TLS itself. |
| `?poll_ms=` | `1000` | How often to poll for completion. |
| `?timeout_ms=` | `300000` | How long to wait for a query. |
| `?arraysize=` | `1000` | Rows fetched per page. |

## Table names

thunderduck names are `catalog.schema.table` (flat-file catalogs) or
`catalog.table` (Iceberg/Nessie catalogs). SQLAlchemy models two levels, so
**the schema is the whole dotted prefix**:

```python
from sqlalchemy import MetaData, Table

md = MetaData()
orders = Table("orders", md, schema="tndr_postgres_demo.ecommerce", autoload_with=engine)  # 3-level
iris = Table("iris", md, schema="tndr_glue_demo", autoload_with=engine)  # 2-level
```

Reflection (`inspect(engine).get_schema_names()` etc.) reads thunderduck's
catalog API rather than issuing SQL, so browsing schemas is fast and free.

## Apache Superset

Installing the package is all that is needed — it registers a Superset DB
engine spec automatically. Add a database of type **Other** with:

```
thunderduck://:tdk_your_token@api.thunderduck.ai/
```

Superset's stop button genuinely cancels the running query.

## How it works, and what follows from that

Every statement is submitted to thunderduck's REST API, which runs it as an
isolated Kubernetes Job; results are then paged back over HTTP. That model has
consequences worth knowing up front:

- **Latency.** Expect seconds, not milliseconds, before the first row. Built
  for analytics and BI, not for tight interactive loops.
- **Read-only.** No DML, no DDL, no transactions. `commit()` and `rollback()`
  are no-ops.
- **Use `NullPool`.** A connection holds no server-side state, so pooling buys
  nothing. The driver also never issues a `SELECT 1` health check, because
  that would start a whole Job.
- **Parameters are rendered client-side.** thunderduck's API accepts SQL only,
  so this driver renders bound parameters into the statement, escaping them
  strictly and refusing types it does not recognise. Always pass untrusted
  values as parameters — never build SQL by string concatenation. The
  paramstyle is `pyformat`, so — as with psycopg2 — a literal percent sign in
  hand-written SQL must be escaped as `%%` when you pass parameters.
- **Row ceiling.** The server caps how many rows one page returns and how many
  a result set stores. Very large result sets are truncated; aggregate in SQL
  rather than pulling raw rows.

## Also available

For JVM tools (DBeaver, DataGrip) there is a
[JDBC driver](https://github.com/thunderduck-io/thunderduck-jdbc), and for .NET
there is [Thunderduck.Data](https://github.com/thunderduck-io/thunderduck-dotnet).

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

One gotcha: the committed `uv.lock` resolves SQLAlchemy 2.x, so a plain
`uv run pytest` only exercises **one** of the two supported majors. Apache
Superset pins `sqlalchemy<2`, so check that combination too before releasing:

```bash
uv venv --python 3.10 .sa14
uv pip install --python .sa14 -e . "sqlalchemy<2" pytest
.sa14/bin/python -c "import sqlalchemy; print('SQLALCHEMY', sqlalchemy.__version__)"
.sa14/bin/pytest -q
```

CI runs both legs on every push.

## License

Apache-2.0. See [LICENSE](LICENSE).

This repository is a mirror of Thunderduck's internal source; see
[CONTRIBUTING.md](CONTRIBUTING.md) for how issues and pull requests are handled,
and [SECURITY.md](SECURITY.md) to report a vulnerability.
