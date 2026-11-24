# Contributing

Thanks for taking the time to look at `thunderduck-sqlalchemy`.

## This repository is a mirror

The source of truth for this driver lives in Thunderduck's internal monorepo.
This repository is a one-way snapshot: each update replaces the tree with the
current state of the source, as a single commit. That means the history here is
not the development history of the project, and a pull request cannot be merged
directly into it.

## Issues are welcome

Bug reports and feature requests are read here. Please include:

- the driver and SQLAlchemy versions (`pip show thunderduck-sqlalchemy sqlalchemy`);
- the Python version and platform;
- what you ran, what you expected, and what happened;
- if relevant, the smallest SQL statement and connection URL (with the token
  removed) that reproduces it.

For anything that looks like a security problem, use
[SECURITY.md](SECURITY.md) instead of a public issue.

## Pull requests

Pull requests are welcome as a place to discuss a change, and small fixes are
genuinely useful. If a change is accepted, it is applied to the internal source
and appears in the next snapshot; the pull request is then closed rather than
merged, so don't be surprised if your commit does not appear here verbatim.

Please keep a change focused: one behaviour per pull request, with a test that
fails before it and passes after.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The package supports both SQLAlchemy 1.4 and 2.x, and Apache Superset pins
`sqlalchemy<2`. Check that combination too when touching the dialect:

```bash
uv venv --python 3.10 .sa14
uv pip install --python .sa14 -e . "sqlalchemy<2" pytest
.sa14/bin/pytest -q
```

`scripts/smoke.py` talks to a real deployment and needs an API token; the test
suite does not.

## License

By contributing you agree that your contribution is licensed under the Apache
License 2.0, the license this project uses. See [LICENSE](LICENSE).
