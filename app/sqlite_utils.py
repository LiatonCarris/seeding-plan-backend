"""Shared SQLite connection policy for the workbench services.

The API process and test/maintenance processes can briefly touch the same
SQLite file while an app is imported or a schema is initialized.  Python's
default five-second SQLite timeout is too short for that legitimate overlap,
so shared workbench connections routed through this module use one explicit,
bounded wait policy.
"""

from __future__ import annotations

import sqlite3
from os import PathLike
from pathlib import Path
from typing import Union


SQLITE_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MILLISECONDS = 30_000
DatabasePath = Union[str, Path, PathLike[str]]


def connect_sqlite(
    database_path: DatabasePath,
    *,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """Open a SQLite connection with the shared contention policy.

    ``timeout`` covers the driver's lock wait and ``busy_timeout`` also
    applies to statements issued after the connection is opened.  We do not
    change journal mode here: switching journal modes is itself a write and
    can introduce a new lock during process startup.
    """

    connection = sqlite3.connect(
        str(database_path),
        timeout=SQLITE_TIMEOUT_SECONDS,
        check_same_thread=check_same_thread,
    )
    connection.execute(
        "PRAGMA busy_timeout = %d" % SQLITE_BUSY_TIMEOUT_MILLISECONDS
    )
    return connection
