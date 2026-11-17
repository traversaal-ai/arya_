"""Read-only MCP tools over the BUSINESS OWNER'S OWN Postgres database.

Not Arya's `arya` database: the owner pastes the connection string of their
own orders/customers/inventory DB as a connector, and the agent answers
"where is my order" from live rows instead of uploaded documents.

Why this module exists at all: Google and Shopify host MCP servers, nobody
hosts one for a private database. So a small MCP server has to sit between
the worker and that database. It speaks MCP to the worker and SQL to the
owner's DB.

Credential flow: the DSN arrives per request in the `X-Arya-Dsn` header
(the worker reads it from the connections table and puts it there), so this
process stores nothing and one instance serves every saved connection. That
also means it is NOT an authority: it can only reach what the caller's own
DSN already reaches, which is why binding it to loopback is enough.

Read-only is enforced in four independent layers, because one is never
enough with SQL:
  1. the statement must be a single SELECT or WITH (parsed here),
  2. every connection sets `default_transaction_read_only`, so even an
     implicit transaction refuses to write,
  3. the query runs inside an explicit READ ONLY transaction that is always
     rolled back,
  4. `statement_timeout` caps runaway scans, and rows/bytes are capped.
Layers 2 and 3 are Postgres itself refusing, not string matching. The
connection form still recommends a read-only role, and the check endpoint
warns when the DSN belongs to a superuser (who can bypass much of this).
"""

import asyncio
import contextlib
import datetime
import decimal
import json
import logging
import pathlib
import re
import uuid
from collections import OrderedDict
from urllib.parse import urlparse

import asyncpg
from mcp.server.fastmcp import Context, FastMCP

logger = logging.getLogger("mcp.postgres")

DSN_HEADER = "X-Arya-Dsn"

STATEMENT_TIMEOUT_MS = 3000
CONNECT_TIMEOUT_S = 5.0
MAX_ROWS = 50
MAX_BYTES = 8000          # serialized result cap, well under the LLM budget
MAX_POOLS = 8             # distinct DSNs kept warm; LRU beyond that
IDLE_CONNECTION_S = 60.0  # drop idle connections to the owner's DB

# System schemas are noise for a business question and a place to poke at
# credentials; never list or describe them.
_HIDDEN_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_LEADING_SELECT = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# The relation named after each FROM or JOIN, optionally schema-qualified or
# quoted. Deliberately not a SQL parser: it only has to recover the tables a
# model named so a failed query can be answered with their real columns.
_RELATION_REF = re.compile(
    r"\b(?:from|join)\s+"
    r"((?:\"[^\"]+\"|[a-z_][\w$]*)(?:\s*\.\s*(?:\"[^\"]+\"|[a-z_][\w$]*))?)",
    re.IGNORECASE,
)
# Things that legally follow FROM but are not relations.
_NOT_RELATIONS = frozenset({
    "select", "lateral", "only", "values", "unnest", "generate_series",
    "json_each", "jsonb_each", "json_array_elements", "jsonb_array_elements",
})


def relations_in(sql: str) -> list[str]:
    """Bare relation names a query refers to, in order, without duplicates.

    "select a from public.orders o join \"order_items\" i ..." gives
    ["orders", "order_items"]. Subqueries and set-returning functions are
    skipped. Used only to make a failed query's error useful.
    """
    out: list[str] = []
    for m in _RELATION_REF.finditer(_COMMENTS.sub(" ", sql or "")):
        name = m.group(1).rsplit(".", 1)[-1].strip().strip('"').lower()
        if name and name not in _NOT_RELATIONS and name not in out:
            out.append(name)
    return out


class QueryRejectedError(Exception):
    """The SQL failed the guard; the message is spoken back to the caller."""


# ─── connection pools, one per DSN ──────────────────────────────────────────

_pools: "OrderedDict[str, asyncpg.Pool]" = OrderedDict()
_pools_lock: asyncio.Lock | None = None
_pools_loop: asyncio.AbstractEventLoop | None = None
_closing: set[asyncio.Task] = set()  # keeps evicted-pool closes alive


def _state() -> "tuple[OrderedDict[str, asyncpg.Pool], asyncio.Lock]":
    """The pool registry for the CURRENT event loop.

    A connection pool and an asyncio.Lock both belong to the loop that made
    them: reused from another loop they raise or, worse, deadlock the
    shutdown that is trying to close them. That happens whenever this
    process runs more than one server in its life, which is exactly what
    the tests do and what an embedded run would do. So a new loop starts
    with an empty registry, and pools from the dead loop are dropped rather
    than awaited: their sockets went with it.
    """
    global _pools, _pools_lock, _pools_loop
    loop = asyncio.get_running_loop()
    if _pools_loop is not loop or _pools_lock is None:
        _pools = OrderedDict()
        _pools_lock = asyncio.Lock()
        _pools_loop = loop
    return _pools, _pools_lock


async def _pool_for(dsn: str) -> asyncpg.Pool:
    _pools, _pools_lock = _state()
    async with _pools_lock:
        pool = _pools.get(dsn)
        if pool is not None and not pool.is_closing():
            _pools.move_to_end(dsn)
            return pool
        while len(_pools) >= MAX_POOLS:
            _, old = _pools.popitem(last=False)
            task = asyncio.create_task(_close_quietly(old))
            _closing.add(task)
            task.add_done_callback(_closing.discard)
        pool = await asyncpg.create_pool(
            dsn,
            min_size=0,
            max_size=2,
            timeout=CONNECT_TIMEOUT_S,
            max_inactive_connection_lifetime=IDLE_CONNECTION_S,
            server_settings={
                # Layer 2: the session itself refuses writes, so even a
                # statement that slips past the parser cannot change data.
                "default_transaction_read_only": "on",
                "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                "idle_in_transaction_session_timeout": "10000",
                "application_name": "arya-mcp",
            },
        )
        _pools[dsn] = pool
        return pool


async def _close_quietly(pool: asyncpg.Pool) -> None:
    try:
        await pool.close()
    except Exception as e:
        logger.debug(f"pool close skipped: {e}")


async def close_all() -> None:
    """Release every pooled connection. Called on sidecar shutdown, so it
    must finish even when something is wrong: a hung close here would stop
    the process exiting."""
    global _pools_loop
    pools, lock = _state()
    async with lock:
        held = list(pools.values())
        pools.clear()
    _pools_loop = None  # next use starts clean
    for p in held:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(_close_quietly(p), 5)


# ─── errors and values, made speakable ──────────────────────────────────────

def describe_db_error(e: BaseException) -> str:
    """One short sentence a voice agent can say, and an operator can act on."""
    if isinstance(e, asyncpg.InvalidPasswordError):
        return "the database rejected the username or password"
    if isinstance(e, asyncpg.InvalidCatalogNameError):
        return "that database name does not exist on the server"
    if isinstance(e, asyncpg.InsufficientPrivilegeError):
        return "this database user is not allowed to read that"
    if isinstance(e, asyncpg.UndefinedTableError):
        return "no such table in this database"
    if isinstance(e, asyncpg.UndefinedColumnError):
        return "no such column in that table"
    if isinstance(e, asyncpg.QueryCanceledError):
        return f"the query took longer than {STATEMENT_TIMEOUT_MS // 1000} seconds and was stopped"
    if isinstance(e, asyncpg.ReadOnlySQLTransactionError):
        return "this connector is read-only, so that statement was refused"
    if isinstance(e, asyncpg.PostgresSyntaxError):
        return f"the SQL has a syntax error: {getattr(e, 'message', str(e))}"
    if isinstance(e, asyncpg.PostgresError):
        return getattr(e, "message", None) or str(e) or "the database refused the query"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "the database did not answer in time"
    if isinstance(e, OSError):
        # Covers DNS failures and refused connections; str() is the useful part.
        return f"the database could not be reached ({e.strerror or e})"
    text = str(e).strip()
    return text[:200] if text else "the database could not be reached"


def _container_hint(dsn: str) -> str:
    """This process runs in a container, so a `localhost` in the owner's
    connection string points at the container, not at their machine. The
    plain "could not be reached" is true but sends people hunting the wrong
    problem, so name the fix."""
    host = ""
    with contextlib.suppress(Exception):
        host = (urlparse(dsn).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") and pathlib.Path("/.dockerenv").exists():
        return (
            ". Note that localhost here means this container: for a database"
            " on your own machine use host.docker.internal, and for one in"
            " this compose stack use its service name"
        )
    return ""


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        return str(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def _pack(payload: dict) -> str:
    """Compact JSON, trimmed to MAX_BYTES by dropping rows rather than
    truncating mid-string (a half-cut JSON blob confuses the model more
    than a short one)."""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    rows = payload.get("rows")
    while len(text) > MAX_BYTES and isinstance(rows, list) and len(rows) > 1:
        rows.pop()
        payload["truncated"] = True
        payload["returned"] = len(rows)
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return text


# ─── the SQL guard (layer 1) ────────────────────────────────────────────────

def check_sql(sql: str) -> str:
    """Return the statement to run, or raise QueryRejected.

    Comments are stripped for the DECISION only; the original text is what
    reaches Postgres, so a `--` inside a string literal can only make this
    stricter, never looser.
    """
    raw = (sql or "").strip()
    if not raw:
        raise QueryRejectedError("no SQL was given")
    bare = _COMMENTS.sub(" ", raw).strip()
    parts = [p for p in bare.split(";") if p.strip()]
    if len(parts) > 1:
        raise QueryRejectedError(
            "only one statement at a time is allowed; send a single SELECT"
        )
    if not _LEADING_SELECT.match(bare):
        first = (bare.split() or ["that"])[0].lower()
        raise QueryRejectedError(
            f"this connector is read-only, so '{first}' is not allowed;"
            " only SELECT (or WITH ... SELECT) queries run"
        )
    # `SELECT ... INTO x` creates a table. The read-only transaction refuses
    # it anyway; saying so here is clearer than a Postgres error.
    if re.search(r"\binto\s+(?!strict\b)[a-z_\"(]", bare, re.IGNORECASE) and \
            not re.search(r"\bfrom\b[^;]*\binto\b", bare, re.IGNORECASE):
        raise QueryRejectedError(
            "SELECT INTO writes a new table, which this connector cannot do"
        )
    return raw.rstrip().rstrip(";")


# ─── the tools ──────────────────────────────────────────────────────────────

def _dsn_from(ctx: Context) -> str:
    """The DSN for THIS request. Absent means the caller is not Arya's
    worker (or an old worker), which is a configuration bug, not a DB error."""
    request = getattr(ctx.request_context, "request", None)
    dsn = ""
    if request is not None:
        try:
            dsn = (request.headers.get(DSN_HEADER) or "").strip()
        except Exception:  # not an HTTP transport
            dsn = ""
    if not dsn:
        raise QueryRejectedError(
            "no database connection was supplied with this request"
        )
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise QueryRejectedError(
            "the saved connection string is not a postgres:// URL"
        )
    return dsn


async def _fetch(dsn: str, sql: str, *args, limit: int | None = None) -> list[asyncpg.Record]:
    """Run one read-only statement and return at most `limit` rows."""
    pool = await _pool_for(dsn)
    async with pool.acquire() as conn:
        # Layer 3: explicit READ ONLY, always rolled back.
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
            if limit is None:
                return await conn.fetch(sql, *args)
            cursor = await conn.cursor(sql, *args)
            return await cursor.fetch(limit)
        finally:
            await tr.rollback()


# ─── what the tools actually do ─────────────────────────────────────────────
# Split from the MCP wrappers below, as in calcom.py, so the behaviour can be
# tested by calling a function with a DSN instead of standing up a server.

MAX_COLUMNS_LISTED = 30   # per relation in list_tables; enough for any view

EMPTY_RESULT = (
    "The query ran and matched no rows. This is a real empty result, not a"
    " failed query."
)
RETRY_NOTE = (
    "Fix the SQL using these columns and run it again. This was a query"
    " error, not an empty result: do not tell the caller that nothing was"
    " found."
)


async def _relations_available(dsn: str) -> list[asyncpg.Record]:
    return await _fetch(
        dsn,
        "SELECT c.table_schema AS schema, c.table_name AS table,"
        " CASE t.table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind,"
        " array_agg(c.column_name::text ORDER BY c.ordinal_position) AS columns"
        " FROM information_schema.columns c"
        " JOIN information_schema.tables t"
        "   ON t.table_schema = c.table_schema"
        "  AND t.table_name = c.table_name"
        " WHERE c.table_schema <> ALL($1::text[])"
        "   AND t.table_type IN ('BASE TABLE', 'VIEW')"
        " GROUP BY 1, 2, 3 ORDER BY 3 DESC, 1, 2",
        list(_HIDDEN_SCHEMAS),
        limit=200,
    )


async def _explain_bad_reference(dsn: str, sql: str, e: BaseException) -> str:
    """A failed query answered with what WOULD have worked.

    Postgres says only 'column "item_name" does not exist'. On its own that
    sent the model straight to "no orders found". So the reply carries the
    real columns of every relation the query named, or the relations that
    do exist when the table itself was wrong, and says in plain words that
    the query failed rather than returned nothing.
    """
    why = getattr(e, "message", None) or describe_db_error(e)
    parts = [f"Query failed: {why}."]
    named = relations_in(sql)
    with contextlib.suppress(Exception):
        available = await _relations_available(dsn)
        by_name = {r["table"].lower(): r for r in available}
        shown = [by_name[n] for n in named if n in by_name]
        if shown:
            parts.append("The relations in your query have these columns:")
            for r in shown:
                cols = list(r["columns"])[:MAX_COLUMNS_LISTED]
                parts.append(f"  {r['table']} ({r['kind']}): {', '.join(cols)}")
        missing = [n for n in named if n not in by_name]
        if missing:
            names = ", ".join(
                f"{r['table']} ({r['kind']})" for r in available[:40]
            )
            parts.append(
                f"No relation called {', '.join(missing)} exists. Available:"
                f" {names}."
            )
    parts.append(RETRY_NOTE)
    return "\n".join(parts)


async def do_list_tables(dsn: str) -> str:
    try:
        rows = await _relations_available(dsn)
    except QueryRejectedError as e:
        return f"Cannot list tables: {e}."
    except Exception as e:
        logger.warning(f"list_tables failed: {e!r}")
        return f"Cannot list tables: {describe_db_error(e)}."
    if not rows:
        return "This database has no tables the connected user can see."
    return _pack({
        "tables": [
            {
                "schema": r["schema"], "table": r["table"], "kind": r["kind"],
                "columns": list(r["columns"])[:MAX_COLUMNS_LISTED],
            }
            for r in rows
        ],
        "returned": len(rows),
        "note": "views are shaped for answering questions; prefer them",
    })


async def do_describe_table(dsn: str, table: str) -> str:
    try:
        name = (table or "").strip().strip('"')
        if not name:
            raise QueryRejectedError("no table name was given")
        schema, _, bare = name.rpartition(".")
        rows = await _fetch(
            dsn,
            "SELECT table_schema AS schema, table_name AS table,"
            " column_name AS column, data_type AS type,"
            " (is_nullable = 'YES') AS nullable"
            " FROM information_schema.columns"
            " WHERE table_name = $1 AND table_schema <> ALL($2::text[])"
            "   AND ($3 = '' OR table_schema = $3)"
            " ORDER BY table_schema, ordinal_position",
            bare, list(_HIDDEN_SCHEMAS), schema,
            limit=300,
        )
    except QueryRejectedError as e:
        return f"Cannot describe that table: {e}."
    except Exception as e:
        logger.warning(f"describe_table failed: {e!r}")
        return f"Cannot describe that table: {describe_db_error(e)}."
    if not rows:
        return (
            f"No table named '{table}' is visible to the connected user."
            " Use list_tables to see what exists."
        )
    return _pack({
        "table": f"{rows[0]['schema']}.{rows[0]['table']}",
        "columns": [
            {"name": r["column"], "type": r["type"], "nullable": r["nullable"]}
            for r in rows
        ],
    })


async def do_run_query(dsn: str, sql: str) -> str:
    statement = ""
    try:
        statement = check_sql(sql)
        rows = await _fetch(dsn, statement, limit=MAX_ROWS + 1)
    except QueryRejectedError as e:
        return f"Query not run: {e}."
    except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError) as e:
        # The one failure a model can fix by itself, if it is told how.
        return await _explain_bad_reference(dsn, statement or sql, e)
    except Exception as e:
        logger.warning(f"run_query failed: {e!r}")
        return f"Query failed: {describe_db_error(e)}. {RETRY_NOTE}"
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    if not rows:
        return EMPTY_RESULT
    payload = {
        "rows": [{k: _jsonable(v) for k, v in r.items()} for r in rows],
        "returned": len(rows),
    }
    if truncated:
        payload["truncated"] = True
        payload["note"] = f"only the first {MAX_ROWS} rows are shown"
    return _pack(payload)


def build(security=None) -> FastMCP:
    """The FastMCP app for this module. Tool docstrings are the contract the
    LLM sees, so they name the read-only limits out loud: a model that knows
    it cannot write stops offering to.

    `security` is the transport's Host/Origin allowlist, built by
    mcp_server: without it the SDK allows localhost only and answers the
    api container's `Host: arya-mcp:8020` with 421.
    """
    app = FastMCP("arya-postgres", stateless_http=True, transport_security=security)

    @app.tool()
    async def list_tables(ctx: Context) -> str:
        """List the tables and views in the connected business database, each
        with its column names.

        Use this FIRST when you do not know what data exists. Views are
        marked kind "view": they are built to answer questions directly, so
        prefer a view over joining tables yourself. Because the column names
        are included you usually do not need describe_table. Read-only.
        """
        try:
            dsn = _dsn_from(ctx)
        except QueryRejectedError as e:
            return f"Cannot list tables: {e}."
        return await do_list_tables(dsn)

    @app.tool()
    async def describe_table(ctx: Context, table: str) -> str:
        """Show the columns and types of one table or view before querying it.

        Args:
            table: Table or view name, optionally schema-qualified
                ("public.orders" or just "orders").
        """
        try:
            dsn = _dsn_from(ctx)
        except QueryRejectedError as e:
            return f"Cannot describe that table: {e}."
        return await do_describe_table(dsn, table)

    @app.tool()
    async def run_query(ctx: Context, sql: str) -> str:
        """Run one read-only SQL query against the business database and
        return the rows.

        Only a single SELECT (or WITH ... SELECT) is allowed: this connector
        cannot insert, update or delete anything. Add a WHERE clause to keep
        results small; at most 50 rows come back.

        Read the reply carefully, because two outcomes look alike and mean
        opposite things:
        - "Query failed": YOUR SQL WAS WRONG. The reply names the bad column
          or table and lists the real columns. Fix the query and run it
          again. Never tell the caller nothing was found because of this.
        - "matched no rows": the query was fine and there is genuinely no
          matching data. Only then may you say nothing was found.

        Args:
            sql: One SELECT statement. No semicolon-separated batches.
        """
        try:
            dsn = _dsn_from(ctx)
        except QueryRejectedError as e:
            return f"Query not run: {e}."
        return await do_run_query(dsn, sql)

    return app


# ─── operational check (used by the API when a connection is saved) ─────────

# The tables the built-in order desk (tools._shop_order_tools) was written
# for. A database that has all four in `public` is reported `shop_desk`, and
# the desk then runs over it instead of being switched off by the connector
# (connectors.served_toolsets). Any other database still replaces the desk.
SHOP_TABLES = ("customers", "orders", "order_items", "returns")


async def check_dsn(dsn: str) -> dict:
    """Can we actually reach this database, and is the account safe?

    Listing MCP tools proves nothing about a DSN (the tool list is static),
    so the API calls this before storing a connection: it is the difference
    between "saved" and "saved and working".
    """
    dsn = (dsn or "").strip()
    if not dsn.startswith(("postgres://", "postgresql://")):
        return {"ok": False, "error": "the connection string must start with postgres://"}
    conn = None
    hint = _container_hint(dsn)
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(
                dsn,
                timeout=CONNECT_TIMEOUT_S,
                server_settings={
                    "application_name": "arya-mcp-check",
                    "statement_timeout": str(STATEMENT_TIMEOUT_MS),
                },
            ),
            CONNECT_TIMEOUT_S + 2,
        )
        info = await conn.fetchrow(
            "SELECT current_database() AS db, current_user AS usr,"
            " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
            "   AS superuser,"
            " (SELECT count(*)::int FROM information_schema.tables"
            "   WHERE table_schema <> ALL($1::text[])) AS tables",
            list(_HIDDEN_SCHEMAS),
        )
        shop_tables = await conn.fetchval(
            "SELECT count(*)::int FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = ANY($1::text[])",
            list(SHOP_TABLES),
        )
    except Exception as e:
        return {"ok": False, "error": describe_db_error(e) + hint}
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.close(), 3)
    warnings = []
    if info["superuser"]:
        warnings.append(
            f"'{info['usr']}' is a superuser. Create a read-only role for the"
            " agent instead: a superuser can bypass the read-only guard."
        )
    if not info["tables"]:
        warnings.append(
            "this user cannot see any tables, so the agent will have nothing"
            " to query"
        )
    return {
        "ok": True,
        "db": info["db"],
        "user": info["usr"],
        "superuser": bool(info["superuser"]),
        "tables": info["tables"],
        "label": f"{info['usr']}@{info['db']}",
        "warnings": warnings,
        "shop_desk": shop_tables == len(SHOP_TABLES),
    }
