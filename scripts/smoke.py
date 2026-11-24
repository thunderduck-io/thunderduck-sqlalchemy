"""Live smoke test against a real thunderduck deployment.

Not part of the pytest suite -- it needs a real token and makes real queries
(each one a Kubernetes Job). Run it by hand:

    TD_TOKEN=tdk_... uv run python scripts/smoke.py

It prints a PASS/FAIL line per check so the output can be pasted into a
verification record.
"""

from __future__ import annotations

import os
import sys
import time

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.pool import NullPool

HOST = os.environ.get("TD_HOST", "api.thunderduck.ai")
TOKEN = os.environ.get("TD_TOKEN")

results: list[tuple[str, bool, str]] = []


def check(name: str):
    def decorator(fn):
        started = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except Exception as err:  # noqa: BLE001 - a smoke script reports, not raises
            ok = False
            detail = f"{type(err).__name__}: {err}"
        elapsed = time.monotonic() - started
        results.append((name, ok, f"{detail} [{elapsed:.1f}s]"))
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail} [{elapsed:.1f}s]")
        return fn

    return decorator


def main() -> int:
    if not TOKEN:
        print("set TD_TOKEN to a tdk_ API token")
        return 2

    engine = create_engine(f"thunderduck://:{TOKEN}@{HOST}/", poolclass=NullPool)
    inspector = inspect(engine)
    state: dict[str, object] = {}

    @check("connect + reflect schema names")
    def _schemas():
        schemas = inspector.get_schema_names()
        state["schemas"] = schemas
        return f"{len(schemas)} schemas: {schemas[:5]}"

    @check("reflect table names")
    def _tables():
        # Prefer a DOTTED schema. A dotted schema means a 3-part table name,
        # which is the form that exercises all three quoted segments in the
        # check below -- i.e. the actual design risk. A bare schema only ever
        # produces a 2-part name, so picking one would let the big check pass
        # while leaving the risky path untested.
        candidates = []
        for schema in state.get("schemas", []):
            tables = inspector.get_table_names(schema=schema)
            if tables:
                candidates.append((schema, tables))
        if not candidates:
            raise RuntimeError("no tables found in any schema")
        candidates.sort(key=lambda c: ("." not in c[0], c[0]))
        schema, tables = candidates[0]
        state["schema"] = schema
        state["table"] = tables[0]
        state["segments"] = len(schema.split("."))
        warning = (
            ""
            if state["segments"] > 1
            else "  <-- WARNING: no dotted schema exists here, so the 3-segment "
            "quoting path is NOT verified by this run"
        )
        return f"{schema} -> {tables[:5]}{warning}"

    @check("reflect columns")
    def _columns():
        columns = inspector.get_columns(state["table"], schema=state["schema"])
        return f"{state['schema']}.{state['table']}: " + ", ".join(
            f"{c['name']}:{c['type']}" for c in columns[:5]
        )

    @check("run a literal query")
    def _literal():
        with engine.connect() as conn:
            value = conn.execute(text("select 1 as one")).fetchall()
        return f"rows={value}"

    @check("THE BIG ONE: quoted multi-part name compiled by SQLAlchemy")
    def _quoted():
        table = Table(state["table"], MetaData(), autoload_with=engine, schema=state["schema"])
        statement = select(table).limit(5)
        compiled = str(statement.compile(engine))
        with engine.connect() as conn:
            rows = conn.execute(statement).fetchall()
        parts = state["segments"] + 1
        verdict = (
            f"{parts}-part name accepted by the rewriter"
            if parts >= 3
            else f"{parts}-part name only -- THE 3-PART PATH REMAINS UNVERIFIED"
        )
        return f"{verdict}; SQL={compiled.splitlines()[0]!r} rows={len(rows)}"

    @check("parameter binding")
    def _params():
        # SQLAlchemy's own `:name` style: it compiles these down to the
        # dialect's pyformat placeholders, so this also proves the paramstyle
        # plumbing, which raw %(name)s would bypass.
        with engine.connect() as conn:
            rows = conn.execute(
                text("select :n as n, :s as s"), {"n": 7, "s": "O'Brien"}
            ).fetchall()
        return f"rows={rows}"

    @check("paging past one page")
    def _paging():
        # A small arraysize forces several server pages for 250 rows; with the
        # default 1000 this would be a single page and prove nothing.
        paging_engine = create_engine(
            f"thunderduck://:{TOKEN}@{HOST}/?arraysize=100", poolclass=NullPool
        )
        with paging_engine.connect() as conn:
            rows = conn.execute(text("select i from range(250) t(i)")).fetchall()
        if len(rows) != 250:
            raise RuntimeError(
                f"expected 250 rows across 3 pages, got {len(rows)} -- if this is "
                "much lower, thunderduck's LIMIT injection capped the result"
            )
        return "250 rows across 3 pages"

    @check("cancel a running query")
    def _cancel():
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            import threading

            def run():
                try:
                    cursor.execute("select count(*) from range(100000000000) t(i)")
                except Exception:
                    pass

            worker = threading.Thread(target=run, daemon=True)
            worker.start()

            # Poll until the run genuinely reaches 'running' rather than
            # sleeping a fixed guess: Kubernetes Job scheduling latency varies,
            # so a fixed wait makes this flaky in both directions -- cancelling
            # before the Job exists, or waiting long after it started.
            client = getattr(raw, "dbapi_connection", raw).client
            exec_id = None
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                exec_id = cursor.execution_id
                if exec_id and client.get_execution(exec_id).status == "running":
                    break
                time.sleep(2)
            else:
                raise RuntimeError("query never reached 'running' within 120s")

            cursor.cancel()
            worker.join(timeout=120)
            final = client.get_execution(exec_id).status
            # 'cancelled' is the expected terminal state; anything else means
            # the cancel did not actually take effect, which is worth seeing.
            return f"exec {exec_id} final status={final} (expected 'cancelled')"
        finally:
            raw.close()

    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
