"""SQLite access. One file, WAL mode, versioned migrations.

WAL matters: the ingest services write while the dashboard and the reflection job read,
and WAL lets readers proceed without blocking the single writer. Everything is same-host
for now; moving to Postgres later means swapping this module and keeping the schema.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kaiba.core.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_local = threading.local()

#: After SQLite resets the WAL it truncates the file back to this. Without it the file
#: stays at its high-water mark forever: on 2026-09-29 it was 19.8 GB, larger than the
#: database, and it filled the disk. ``kaiba.ops.db_guard`` forces the reset itself.
WAL_SIZE_LIMIT_BYTES = 512 * 1024 * 1024


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(f"PRAGMA journal_size_limit={WAL_SIZE_LIMIT_BYTES}")


class _Connection(sqlite3.Connection):
    """A connection that carries its own transaction lock. See :func:`tx`.

    The lock has to live on the connection, and a plain ``sqlite3.Connection`` is a C type
    with neither ``__dict__`` nor weak-reference support, so there is nowhere to hang it
    and no way to key a side table off it that a long-running service can ever prune. A
    subclass has both.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Reentrant because :func:`tx` nests *within* a thread by design (SAVEPOINT); it
        #: must only ever block a DIFFERENT thread.
        self.kaiba_tx_lock = threading.RLock()


#: Transaction lock for connections this module did not create: the handles opened with a
#: bare ``sqlite3.connect`` (the read-only study readers in :mod:`kaiba.learning`, the
#: importer in :mod:`kaiba.intelligence.naming`, replay's in-memory scratch). They have
#: nowhere to carry a lock of their own, so they share this one: coarser than each needs,
#: and correct, which is the right way round. A proxy that forwards attribute lookups --
#: the shape ``tests/test_nested_transactions.py`` uses -- reaches the real connection's
#: lock instead, which is the answer we want there.
_foreign_tx_lock = threading.RLock()


def tx_lock(conn: sqlite3.Connection) -> threading.RLock:
    """The lock :func:`tx` holds for the life of a transaction on ``conn``."""
    lock = getattr(conn, "kaiba_tx_lock", None)
    return _foreign_tx_lock if lock is None else lock


def connect(path: Path | None = None) -> sqlite3.Connection:
    """A fresh connection. Callers that hold one for a while should use :func:`session`."""
    p = path or get_settings().db_path
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        p, timeout=10, isolation_level=None, check_same_thread=False, factory=_Connection
    )
    _configure(conn)
    return conn


def get_conn(path: Path | None = None) -> sqlite3.Connection:
    """Thread-local connection, created on first use in each thread."""
    existing = getattr(_local, "conn", None)
    if existing is None:
        existing = connect(path)
        _local.conn = existing
    return existing


def close_thread_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


#: Serial number for savepoint names, so nested blocks cannot collide.
_savepoint_seq = itertools.count()


@contextmanager
def tx(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Explicit transaction that NESTS. Rolls back its own work on exception.

    Reentrant since 2026-09-22. It used to open with an unconditional ``BEGIN IMMEDIATE``
    and close a failure with an unconditional ``ROLLBACK``, which broke in two ways the
    moment a caller already held a transaction:

    * the ``BEGIN`` raised ``cannot start a transaction within a transaction``, so the
      body never ran -- and a caller whose body was a resource RELEASE leaked the
      resource every single time (see :func:`kaiba.core.limiter.release`, which was
      burning provider slots on the live box at roughly ten an hour);
    * the ``ROLLBACK`` then threw away the CALLER's uncommitted writes, because there was
      no transaction of ours to roll back -- only theirs. Silently, under a log line
      about something else entirely.

    When a transaction is already open we take a SAVEPOINT instead. That nests, and
    rolling back to it undoes our work while leaving the enclosing transaction intact and
    usable. Only the outermost block commits, which is the semantics callers already
    assumed they had.

    A block also holds the connection's own lock for its whole extent, so threads sharing
    a connection queue rather than interleave. "Already open" is otherwise a question
    about the connection that each thread answers with the other's transaction; the
    comment below has the two ways that goes wrong.
    """
    c = conn or get_conn()

    # ONE THREAD AT A TIME PER CONNECTION, for the whole block. `in_transaction` answers
    # for the CONNECTION, not for the calling thread, and our connections are shareable
    # (`connect` passes `check_same_thread=False`; the watchdog prices the book across
    # QUOTE_PREFETCH_WORKERS threads). Two threads on one connection therefore read each
    # other's transaction as their own enclosing block, and BOTH paths below then misfire:
    #
    # * the outer path -- both see no transaction, both BEGIN, and the loser is refused;
    # * the nested path -- the loser takes a SAVEPOINT inside the WINNER's transaction,
    #   the winner COMMITs, and the loser's RELEASE dies with `no such savepoint`, its
    #   work having been committed by a transaction it does not own, on someone else's
    #   schedule.
    #
    # Attempting the BEGIN and reading its refusal (below) answers the first, because that
    # race is decided inside one statement. It CANNOT answer the second: nothing the loser
    # can check tells it that the winner will commit while its savepoint is open. Only
    # holding the connection for the duration does, which is what this lock is.
    #
    # Reentrant, because nesting within one thread is the designed behaviour here, and
    # uncontended for the thread-local connections `get_conn` hands out -- which is all
    # but the callers that deliberately share one.
    with tx_lock(c):
        # Ask forgiveness, not permission: taking `in_transaction` as gospel is a
        # check-then-act race for any connection whose flag this lock does not cover --
        # a proxy, or a handle opened outside this module. MEASURED on the live box
        # 2026-09-22: after the first version of this fix shipped, `limiter could not
        # release the <provider> slot: cannot start a transaction within a transaction`
        # still appeared three times in four minutes. The refusal is the answer to the
        # question, and it is the same answer the flag would have given.
        owns_transaction = False
        if not c.in_transaction:
            try:
                c.execute("BEGIN IMMEDIATE")
                owns_transaction = True
            except sqlite3.OperationalError as exc:
                if "within a transaction" not in str(exc):
                    raise

        if not owns_transaction:
            name = f"kaiba_sp_{next(_savepoint_seq)}"
            c.execute(f"SAVEPOINT {name}")
            try:
                yield c
            except Exception:
                # ROLLBACK TO rewinds to the savepoint but does NOT pop it; RELEASE pops
                # it. Both are needed or the savepoint stack grows for the life of the
                # outer transaction.
                c.execute(f"ROLLBACK TO {name}")
                c.execute(f"RELEASE {name}")
                raise
            else:
                c.execute(f"RELEASE {name}")
            return
        try:
            yield c
        except Exception:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")


# --------------------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------------------


def _applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  name TEXT PRIMARY KEY, applied_at_ms INTEGER NOT NULL)"
    )
    return {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}


def migrate(conn: sqlite3.Connection | None = None, verbose: bool = False) -> list[str]:
    """Apply every unapplied ``NNN_*.sql`` in order. Returns the names applied."""
    from kaiba.core.schemas import now_ms

    c = conn or get_conn()
    done = _applied(c)
    applied: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.name in done:
            continue
        c.executescript(sql_file.read_text(encoding="utf-8"))
        c.execute(
            "INSERT INTO schema_migrations(name, applied_at_ms) VALUES (?,?)",
            (sql_file.name, now_ms()),
        )
        applied.append(sql_file.name)
        if verbose:
            print(f"applied {sql_file.name}")
    return applied


def ensure_db(path: Path | None = None) -> sqlite3.Connection:
    conn = get_conn(path)
    migrate(conn)
    return conn


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def jdump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def jload(value: str | None, default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default if default is not None else {}


def upsert(
    conn: sqlite3.Connection,
    table: str,
    row: dict[str, Any],
    conflict: Iterable[str],
    update: Iterable[str] | None = None,
) -> None:
    cols = list(row)
    placeholders = ",".join("?" for _ in cols)
    conflict_cols = ",".join(conflict)
    updates = list(update) if update is not None else [c for c in cols if c not in set(conflict)]
    set_clause = ",".join(f"{c}=excluded.{c}" for c in updates) or f"{cols[0]}={cols[0]}"
    conn.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_cols}) DO UPDATE SET {set_clause}",
        [row[c] for c in cols],
    )


def fetch_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, tuple(params))]


def fetch_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None
