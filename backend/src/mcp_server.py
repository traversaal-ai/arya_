"""arya-mcp: the sidecar that hosts Arya's own MCP servers.

Run by the `arya-mcp` compose service from the same image as the api and
the worker:

    uv run python src/mcp_server.py        # 0.0.0.0:8020 inside the container

Why a separate process rather than tools inside the worker: the product rule
is that everything external is reached through MCP tools (the agent never
opens a database connection itself), and vendors like Google or Shopify host
their MCP servers for us. Nobody hosts one for a customer's private Postgres,
so Arya hosts it. One long-lived process serves every call, unlike a stdio
server which would cold-start per call (one worker subprocess per call).

Each module mounts under its own prefix, so `mcp_tools/postgres_readonly.py`
is reachable at `/postgres/mcp` and the connectors catalog points at it with
`{"url_source": "sidecar", "path": "/postgres/mcp"}`.

Credentials NEVER live here: they arrive per request in a header (the DSN in
`X-Arya-Dsn`), read by the worker out of the connections table. So this
process holds no authority of its own and can only reach what the caller's
own credential already reaches. That is what makes a loopback-bound port
safe enough: compose publishes it on 127.0.0.1 only, because the worker runs
with `network_mode: host` and must reach it over localhost.

Alongside each MCP mount there is one plain HTTP route the API uses when a
connection is saved (`POST /postgres/check`): listing MCP tools proves
nothing about a credential, since the tool list is static, so the check is
what turns "saved" into "saved and working" with a readable reason on
failure.
"""

import asyncio
import contextlib
import logging
import os

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp_tools import calcom, duffel, postgres_readonly

logging.basicConfig(
    level=os.environ.get("ARYA_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("arya-mcp")

# Who may address this service, by Host header. The MCP SDK enforces this
# (DNS rebinding protection: a browser tricked into calling a local MCP
# server is a real attack) and allows ONLY localhost unless told otherwise,
# so Arya's own callers have to be named or every request from the api
# container comes back 421 Misdirected Request:
#   localhost / 127.0.0.1   the worker (network_mode: host) and host curl
#   arya-mcp                the api container over the compose network
#   host.docker.internal    a source-run api against the containerized sidecar
# Keep the protection ON and extend this list rather than disabling it;
# ARYA_MCP_ALLOWED_HOSTS (comma-separated) covers a renamed service.
_DEFAULT_HOSTS = ("localhost", "127.0.0.1", "[::1]", "arya-mcp",
                  "host.docker.internal")


def _security() -> TransportSecuritySettings:
    raw = os.environ.get("ARYA_MCP_ALLOWED_HOSTS", "")
    hosts = [h.strip() for h in raw.split(",") if h.strip()] or list(_DEFAULT_HOSTS)
    # Both the bare name and any port: compose publishes 8020, a source run
    # may pick another.
    allowed = [h for host in hosts for h in (host, f"{host}:*")]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=[f"{scheme}{host}" for host in allowed
                         for scheme in ("http://", "https://")],
    )


# One entry per hosted module: the mount prefix and its FastMCP app. Adding
# a module (calcom, duffel, shopify_admin) is one line here plus a catalog
# entry with the matching `path`.
def build_modules() -> dict:
    security = _security()
    return {
        "postgres": postgres_readonly.build(security=security),
        "calcom": calcom.build(security=security),
        "duffel": duffel.build(security=security),
    }


def health_for(modules: dict):
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "service": "arya-mcp",
            "modules": sorted(modules),
        })

    return health


async def postgres_check(request: Request) -> JSONResponse:
    """Validate a Postgres DSN for the API's save/Test buttons.

    Never 500s and never echoes the DSN back: the response is
    {ok, db, user, superuser, tables, label, warnings} or {ok: false, error}.
    """
    dsn = (request.headers.get(postgres_readonly.DSN_HEADER) or "").strip()
    if not dsn:
        body = await _json_body(request)
        dsn = (body.get("dsn") or "").strip()
    if not dsn:
        return JSONResponse(
            {"ok": False, "error": "no connection string was supplied"},
            status_code=400,
        )
    try:
        return JSONResponse(await postgres_readonly.check_dsn(dsn))
    except Exception as e:  # never leak a traceback to the API
        logger.warning(f"postgres check failed: {e!r}")
        return JSONResponse(
            {"ok": False, "error": postgres_readonly.describe_db_error(e)}
        )


async def calcom_check(request: Request) -> JSONResponse:
    """Validate a Cal.com API key for the API's save/Test buttons: the key
    is real, and the account has something bookable."""
    raw = (request.headers.get("authorization") or "").strip()
    key = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not key:
        body = await _json_body(request)
        key = (body.get("api_key") or "").strip()
    if not key:
        return JSONResponse(
            {"ok": False, "error": "no API key was supplied"}, status_code=400
        )
    try:
        return JSONResponse(await calcom.check_key(key))
    except Exception as e:
        logger.warning(f"calcom check failed: {e!r}")
        return JSONResponse({"ok": False, "error": "Cal.com could not be reached"})


async def duffel_check(request: Request) -> JSONResponse:
    """Validate a Duffel access token for the API's save/Test buttons, and
    say whether it is a test or a live token: the card shows it and the
    prompt block tells the agent to admit sandbox bookings."""
    raw = (request.headers.get("authorization") or "").strip()
    key = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not key:
        body = await _json_body(request)
        key = (body.get("api_key") or body.get("token") or "").strip()
    if not key:
        return JSONResponse(
            {"ok": False, "error": "no access token was supplied"}, status_code=400
        )
    try:
        return JSONResponse(await duffel.check_key(key))
    except Exception as e:
        logger.warning(f"duffel check failed: {e!r}")
        return JSONResponse({"ok": False, "error": "Duffel could not be reached"})


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def create_app(modules: dict | None = None) -> Starlette:
    """A fresh sidecar app. A factory rather than one module-level instance
    because a FastMCP session manager can only be run once, so a second
    server (a test, an embedded run) needs its own set of modules."""
    modules = modules if modules is not None else build_modules()

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        """Each mounted FastMCP app needs its session manager running, and
        the parent owns that: a mounted sub-app's own lifespan never fires."""
        async with contextlib.AsyncExitStack() as stack:
            for name, mcp_app in modules.items():
                await stack.enter_async_context(mcp_app.session_manager.run())
                logger.info(f"module '{name}' ready at /{name}/mcp")
            try:
                yield
            finally:
                # Per-module cleanup, each capped and swallowed: one module
                # holding a bad connection must not stop the process exiting.
                for closer in (postgres_readonly.close_all,):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(closer(), 10)

    return Starlette(
        routes=[
            Route("/health", health_for(modules)),
            Route("/postgres/check", postgres_check, methods=["POST"]),
            Route("/calcom/check", calcom_check, methods=["POST"]),
            Route("/duffel/check", duffel_check, methods=["POST"]),
            # Mounted last: FastMCP's app owns everything under its prefix.
            *[Mount(f"/{name}", app=m.streamable_http_app())
              for name, m in modules.items()],
        ],
        lifespan=lifespan,
    )


app = create_app()


if __name__ == "__main__":
    uvicorn.run(
        app,
        # 0.0.0.0 inside the container; compose publishes it on 127.0.0.1
        # only, so nothing outside this machine can reach it.
        host=os.environ.get("ARYA_MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("ARYA_MCP_PORT", "8020")),
        log_level=os.environ.get("ARYA_MCP_LOG_LEVEL", "info").lower(),
    )
