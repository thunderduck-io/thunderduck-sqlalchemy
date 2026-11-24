"""SQLAlchemy dialect and DBAPI for thunderduck.ai.

Read-only by design: every statement is submitted to the thunderduck API,
which runs it as a Kubernetes Job, and results are paged back over HTTP. See
README.md for the connection URL format and the caveats that follow from that
model.
"""

from .bulk import fetch_dataframe, fetch_result_url
from .dbapi import connect
from .dialect import ThunderduckDialect

__version__ = "1.0.2"

__all__ = ["__version__", "connect", "ThunderduckDialect", "fetch_result_url", "fetch_dataframe"]
