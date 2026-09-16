"""incidentd — turns Alertmanager webhooks into managed incidents.

The package is deliberately small and dependency-light: FastAPI for the HTTP
edge, Pydantic for payload validation, the stdlib ``sqlite3`` module for
storage (no ORM) and plain dataclasses for the domain.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
