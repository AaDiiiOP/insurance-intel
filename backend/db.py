"""
Database access for Aurevia BrokerOS — compatibility shim.

The database access code that used to live entirely in this file has been
split into the `repositories/` package, organised by feature area
(carriers, clients, matches, gaps, drafts, feedback, submissions,
extraction, plus shared connection/schema setup in
repositories/connection.py).

This module re-exports every one of those names so existing call sites
that do `import db` and call `db.xxx(...)` keep working completely
unchanged. See repositories/__init__.py for the full list, and
repositories/connection.py's module docstring for the organization-scoping
notes that used to live at the top of this file.
"""
from repositories import *  # noqa: F401,F403
from repositories import __all__ as _repositories_all

__all__ = list(_repositories_all)
