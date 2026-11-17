"""The read-only Postgres connector: the SQL guard, result packing, error
wording, and (when a database is reachable) a real round trip through the
sidecar's own MCP server.

The DB-backed tests use ARYA_TEST_DSN, falling back to DATABASE_URL from
backend/.env.local, and skip when neither is reachable. They only ever
read, so pointing them at Arya's own database is safe.
"""

import asyncio
import json
import os
import pathlib
import socket
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import asyncpg
import pytest
from dotenv import load_dotenv

from mcp_tools import postgres_readonly as pg

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env.local")

# ─── the SQL guard ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from orders where id = 3",
    "  \n WITH recent AS (SELECT 1 AS n) SELECT * FROM recent  ",
    "SELECT 1; ",                      # one statement, stray semicolon
    "-- a comment\nSELECT 2",
    "/* block */ SELECT 3",
    "SELECT count(*) FROM orders",
])
def test_check_sql_allows_single_reads(sql):
    out = pg.check_sql(sql)
    assert out and not out.endswith(";")


@pytest.mark.parametrize(("sql", "because"), [
    ("", "no SQL"),
    ("   ", "no SQL"),
    ("DELETE FROM orders", "read-only"),
    ("UPDATE orders SET total = 0", "read-only"),
    ("INSERT INTO orders (id) VALUES (1)", "read-only"),
    ("DROP TABLE orders", "read-only"),
    ("TRUNCATE orders", "read-only"),
    ("GRANT ALL ON orders TO public", "read-only"),
    ("CREATE TABLE t (a int)", "read-only"),
    ("COPY orders FROM PROGRAM 'sh -c whoami'", "read-only"),
    ("SELECT 1; DROP TABLE orders", "one statement"),
    ("SELECT 1; SELECT 2", "one statement"),
    ("SELECT * INTO copy_of FROM orders", "writes a new table"),
])
def test_check_sql_rejects_writes_and_batches(sql, because):
    with pytest.raises(pg.QueryRejectedError) as ei:
        pg.check_sql(sql)
    assert because in str(ei.value)


def test_check_sql_comment_hidden_batch_is_still_rejected():
    # Stripping comments happens before counting statements, so a second
    # statement cannot be smuggled in behind one.
    with pytest.raises(pg.QueryRejectedError):
        pg.check_sql("SELECT 1 -- harmless\n; DROP TABLE orders")


# ─── packing results for a voice agent ──────────────────────────────────────


def test_jsonable_coerces_db_types():
    assert pg._jsonable(Decimal("19.50")) == 19.5
    assert pg._jsonable(date(2026, 9, 9)) == "2026-09-09"
    assert pg._jsonable(datetime(2026, 9, 9, 8, 30, tzinfo=timezone.utc)).startswith("2026-09-09T08:30")
    assert pg._jsonable(UUID("0f3c1b2c-4d5e-6f70-8192-a3b4c5d6e7f8")).startswith("0f3c1b2c")
    assert pg._jsonable(b"\x00\x01\x02") == "<3 bytes>"
    assert pg._jsonable({"a": [Decimal("1")]}) == {"a": [1.0]}
    assert pg._jsonable(None) is None


def test_pack_drops_rows_to_fit_the_byte_cap():
    payload = {"rows": [{"blob": "x" * 500} for _ in range(100)], "returned": 100}
    text = pg._pack(payload)
    assert len(text) <= pg.MAX_BYTES
    body = json.loads(text)
    assert body["truncated"] is True
    assert body["returned"] == len(body["rows"]) < 100
    # Still valid JSON, not a mid-string cut.
    assert body["rows"][0]["blob"].startswith("xxx")


def test_describe_db_error_is_a_sentence_not_a_traceback():
    assert "username or password" in pg.describe_db_error(asyncpg.InvalidPasswordError("nope"))
    assert "read-only" in pg.describe_db_error(asyncpg.ReadOnlySQLTransactionError("no"))
    assert "seconds" in pg.describe_db_error(asyncpg.QueryCanceledError("timeout"))
    assert "did not answer in time" in pg.describe_db_error(asyncio.TimeoutError())
    assert pg.describe_db_error(RuntimeError("")) == "the database could not be reached"


# ─── against a real database ────────────────────────────────────────────────


def _dsn() -> str | None:
    return os.environ.get("ARYA_TEST_DSN") or os.environ.get("DATABASE_URL")


async def _reachable(dsn: str) -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn, timeout=3), 5)
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def dsn():
    d = _dsn()
    if not d or not await _reachable(d):
        pytest.skip("no reachable database (set ARYA_TEST_DSN)")
    yield d
    await pg.close_all()


async def test_check_dsn_reports_the_database_and_user(dsn):
    out = await pg.check_dsn(dsn)
    assert out["ok"] is True
    assert out["db"] and out["user"]
    assert out["label"] == f"{out['user']}@{out['db']}"
    assert isinstance(out["warnings"], list)
    # Arya's own database is not a shop, so the order desk must not adopt it.
    assert out["shop_desk"] is False


@pytest.mark.parametrize(("bad", "because"), [
    ("mysql://root@localhost/db", "postgres://"),
    ("", "postgres://"),
])
async def test_check_dsn_rejects_non_postgres(bad, because):
    out = await pg.check_dsn(bad)
    assert out["ok"] is False and because in out["error"]


async def test_check_dsn_wrong_password_says_so(dsn):
    broken = dsn.replace("://", "://nobody:wrongpass@", 1)
    # Strip the original credentials that now follow the injected ones.
    scheme, _, rest = broken.partition("://")
    injected, _, tail = rest.partition("@")
    host = tail.split("@")[-1]
    out = await pg.check_dsn(f"{scheme}://{injected}@{host}")
    assert out["ok"] is False
    assert "password" in out["error"] or "does not exist" in out["error"]


async def test_read_only_transaction_refuses_a_write(dsn):
    # Proof that layer 2/3 are Postgres refusing, not our parser: bypass
    # check_sql entirely and send a write straight through _fetch.
    with pytest.raises(asyncpg.PostgresError) as ei:
        await pg._fetch(dsn, "CREATE TABLE arya_guard_probe (a int)")
    assert "read-only" in pg.describe_db_error(ei.value)


def test_relations_in_recovers_the_tables_a_query_named():
    sql = (
        "select o.a, i.b from public.orders o"
        ' join "order_items" i on i.order_id = o.order_id'
        " left join customers using (customer_id)"
        " where o.x in (select z from returns) and o.y > 1"
    )
    assert pg.relations_in(sql) == ["orders", "order_items", "customers", "returns"]
    assert pg.relations_in("select 1") == []
    # Set-returning functions and subqueries are not relations.
    assert pg.relations_in("select * from generate_series(1, 3)") == []
    assert pg.relations_in("select * from (select 1) sub") == []
    # A comment mentioning FROM does not count.
    assert pg.relations_in("-- from nowhere\nselect 1 from orders") == ["orders"]


async def test_bad_column_teaches_the_real_columns_and_says_retry(dsn):
    """Seen live: 'Query failed: no such column in that table.' sent the
    model to 'no orders found'. The reply must now name the bad column, list
    what the relation actually has, and say this was not an empty result."""
    out = await pg.do_run_query(dsn, "SELECT no_such_col FROM connections")
    assert out.startswith("Query failed")
    assert 'column "no_such_col" does not exist' in out
    assert "connections (table):" in out
    assert "status" in out and "type" in out, "the real columns are listed"
    assert "run it again" in out
    assert "not an empty result" in out
    assert "do not tell the caller that nothing was found" in out


async def test_bad_table_lists_what_exists(dsn):
    out = await pg.do_run_query(dsn, "SELECT 1 FROM connectionz")
    assert 'relation "connectionz" does not exist' in out
    assert "No relation called connectionz exists" in out
    assert "connections (table)" in out, "the real relations are offered"
    assert "run it again" in out


async def test_an_empty_result_cannot_be_mistaken_for_a_failure(dsn):
    out = await pg.do_run_query(
        dsn, "SELECT id FROM connections WHERE id = 'no-such-row-ever'"
    )
    assert out == pg.EMPTY_RESULT
    assert "real empty result" in out and "not a failed query" in out


async def test_list_tables_carries_kind_and_column_names(dsn):
    out = json.loads(await pg.do_list_tables(dsn))
    conn = next(t for t in out["tables"] if t["table"] == "connections")
    assert conn["kind"] == "table"
    assert "status" in conn["columns"] and "type" in conn["columns"]
    assert "prefer them" in out["note"]


def test_security_allows_aryas_own_callers():
    """Regression: the SDK's DNS-rebinding protection allows localhost only
    by default, so every MCP request from the api container (Host:
    arya-mcp:8020) came back 421 Misdirected Request. The worker, on host
    networking, was unaffected, which is why only a live test caught it."""
    import mcp_server

    s = mcp_server._security()
    assert s.enable_dns_rebinding_protection is True
    for host in ("arya-mcp:8020", "localhost:8020", "127.0.0.1:8020",
                 "host.docker.internal:8020", "arya-mcp"):
        assert host in s.allowed_hosts or f"{host.split(':')[0]}:*" in s.allowed_hosts
    assert "evil.example.com" not in s.allowed_hosts


async def test_mcp_endpoint_accepts_the_api_containers_host_header():
    """The settings test above checks the list; this one checks the wire,
    because the 421 came from middleware, not from our code. No database
    needed: a rejected Host never reaches a tool."""
    import httpx
    import uvicorn

    import mcp_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        mcp_server.create_app(), host="127.0.0.1", port=port, log_level="warning",
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            url = f"http://127.0.0.1:{port}/postgres/mcp"
            # Stream, so only the response HEADERS are read: the MCP
            # endpoint answers with an open text/event-stream body.
            async with c.stream("POST", url, json=body,
                                headers={**headers, "host": "arya-mcp:8020"}) as ok:
                assert ok.status_code == 200, ok.status_code
            async with c.stream("POST", url, json=body,
                                headers={**headers, "host": "evil.example.com"}) as bad:
                assert bad.status_code == 421
    finally:
        server.should_exit = True
        await task


def test_security_allowlist_is_overridable(monkeypatch):
    monkeypatch.setenv("ARYA_MCP_ALLOWED_HOSTS", "mcp.internal, other")
    import mcp_server

    hosts = mcp_server._security().allowed_hosts
    assert "mcp.internal" in hosts and "mcp.internal:*" in hosts
    assert "arya-mcp" not in hosts


async def test_tools_answer_over_mcp(dsn):
    """End to end: run the sidecar app, call its tools over streamable HTTP
    with the DSN in the header, exactly as the worker does."""
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    import mcp_server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        mcp_server.create_app(), host="127.0.0.1", port=port, log_level="warning",
    ))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "sidecar did not start"

    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as plain:
            health = await plain.get(f"http://127.0.0.1:{port}/health")
            assert health.json()["modules"] == ["calcom", "duffel", "postgres"]
            checked = await plain.post(
                f"http://127.0.0.1:{port}/postgres/check",
                headers={pg.DSN_HEADER: dsn},
            )
            assert checked.json()["ok"] is True

        async def call(tool, args):
            async with (
                httpx.AsyncClient(headers={pg.DSN_HEADER: dsn}, timeout=20) as http,
                streamable_http_client(
                    url=f"http://127.0.0.1:{port}/postgres/mcp", http_client=http
                ) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                assert names == ["list_tables", "describe_table", "run_query"]
                res = await session.call_tool(tool, args)
                return res.content[0].text

        tables = await call("list_tables", {})
        assert "connections" in tables and "conversations" in tables

        described = await call("describe_table", {"table": "connections"})
        assert "oauth_state" in described and "status" in described

        rows = await call("run_query", {"sql": "SELECT 1 AS one, 'x' AS s"})
        assert json.loads(rows)["rows"] == [{"one": 1, "s": "x"}]

        # A write is refused with a spoken sentence, not an exception.
        refused = await call("run_query", {"sql": "DELETE FROM connections"})
        assert "read-only" in refused and "not allowed" in refused

        # An unknown table explains itself instead of leaking SQL state.
        missing = await call("describe_table", {"table": "no_such_table_here"})
        assert "list_tables" in missing

        # A row cap applies even when the query asks for everything.
        capped = await call("run_query", {
            "sql": "SELECT generate_series(1, 500) AS n",
        })
        body = json.loads(capped)
        assert body["returned"] == pg.MAX_ROWS and body["truncated"] is True
    finally:
        server.should_exit = True
        await task
        await pg.close_all()
