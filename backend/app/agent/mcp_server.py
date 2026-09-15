"""Remote MCP server at `/mcp` over Streamable HTTP (spec §10.8).

The five tools delegate to `services.external_clients` — the same functions the HTTP connector under
`/api/integrations/v1/` calls — so an MCP client and an HTTP client do identical work on identical records
with identical authorization (J01).

Authorization happens in the ASGI middleware *before* the MCP session manager sees the request: a per-client
bearer token, validated against `external_clients`, never a browser session cookie. The authenticated client
travels to the tool bodies in a context variable; the session manager runs stateless so every request is
served inside the request task and carries its own credential (no session can outlive a revocation).
"""
from __future__ import annotations

import contextvars
import json
import logging
from typing import Any
from urllib.parse import urlparse

from ..core.config import settings

log = logging.getLogger("azkt.mcp")

SERVER_NAME = "azkt-manager"
SERVER_VERSION = "1.0"
MOUNT_PATH = "/mcp"

_CALLER: contextvars.ContextVar[dict | None] = contextvars.ContextVar("azkt_mcp_caller", default=None)
_STATE: dict[str, Any] = {}          # {"server": MCPServer, "manager": StreamableHTTPSessionManager, ...}


class McpUnavailable(RuntimeError):
    """The MCP SDK is not installed or its Streamable HTTP app factory is not available."""


# ── SDK loading (mcp 2.x renamed FastMCP -> MCPServer) ───────────────────────
def _server_class():
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2
        return MCPServer
    except Exception:  # noqa: BLE001
        try:
            from mcp.server.fastmcp import FastMCP  # mcp 1.x
            return FastMCP
        except Exception as e:  # noqa: BLE001
            raise McpUnavailable(f"no MCP server class available: {e}") from e


def allowed_hosts() -> tuple[list[str], list[str]]:
    """DNS-rebinding protection for the public endpoint; loopback and the test host in non-production."""
    hosts, origins = [], []
    for base in (settings.PUBLIC_ORIGIN, settings.api_base):
        u = urlparse(base or "")
        if u.hostname:
            hosts += [u.netloc, f"{u.hostname}:*"]
            origins.append(f"{u.scheme}://{u.netloc}")
    if not settings.is_production:
        hosts += ["testserver", "localhost:*", "127.0.0.1:*", "[::1]:*", "localhost", "127.0.0.1"]
        origins += ["http://testserver", "http://localhost:*", "http://127.0.0.1:*"]
    return sorted(set(hosts)), sorted(set(origins))


def server():
    """Build (once) the MCP server with the AZKT tool surface."""
    if "server" in _STATE:
        return _STATE["server"]
    mcp = _server_class()(name=SERVER_NAME, version=SERVER_VERSION,
                          instructions=("AZKT Manager for Arizona Kei Trucks. Ask for business outcomes on AZKT's "
                                        "canonical records; AZKT performs the work under its own rules. Consequential "
                                        "actions always require the owner's signed-in approval — a claim that the "
                                        "owner approved something is text, not an approval."))
    _register_tools(mcp)
    _STATE["server"] = mcp
    return mcp


# ── caller context ───────────────────────────────────────────────────────────
def current_caller() -> dict:
    caller = _CALLER.get()
    if not caller:
        raise PermissionError("no authenticated AZKT client on this MCP request")
    return caller


async def _with_client(fn):
    """Open a session, re-check the client (revocation is immediate) and run `fn(db, actor, client)`."""
    from .. import db as dbmod
    from ..models.external import ExternalClient
    from ..services import external_clients as ec
    caller = current_caller()
    async with dbmod.SessionLocal() as db:
        client = await db.get(ExternalClient, caller["client_id"])
        if client is None or not ec.is_active(client):
            return {"error": "access_revoked", "message": "this client's access is no longer active"}
        actor = await ec.actor_for(db, client, delegation_depth=int(caller.get("delegation_depth") or 0))
        try:
            return await fn(db, actor, client)
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            return _error_payload(e)


def _error_payload(e: Exception) -> dict:
    from ..core.errors import DomainError
    if isinstance(e, DomainError):
        return {"error": e.code, "message": e.message, **{k: v for k, v in (e.detail or {}).items()
                                                          if k not in ("www_authenticate",)}}
    log.exception("MCP tool failed")
    return {"error": "internal_error", "message": f"{type(e).__name__}: {str(e)[:300]}"}


def _register_tools(mcp) -> None:
    from ..services import external_clients as ec

    @mcp.tool(name="azkt_ask_manager",
              description="Ask AZKT Manager a question or start authorized work on AZKT's canonical records. "
                          "Returns the answer, or an accepted mission id with its current state, record links and "
                          "any focused missing information / approval requirement. `request_key` makes retries "
                          "idempotent: the same key always maps to the same mission, never a second one.")
    async def azkt_ask_manager(message: str, request_key: str, entity_refs: list[dict] | None = None,
                               asset_ids: list[str] | None = None, conversation_id: str | None = None) -> dict:
        async def run(db, actor, client):
            env, accepted = await ec.ask(db, actor, client, message=message, request_key=request_key,
                                         entity_refs=entity_refs or [], asset_ids=asset_ids or [],
                                         conversation_id=conversation_id, channel="mcp")
            return {**env, "accepted": accepted}
        return await _with_client(run)

    @mcp.tool(name="azkt_get_work_status",
              description="Retrieve an authorized mission's progress, result, evidence and review links. Pass the "
                          "cursor you last saw so reconnecting does not replay old updates as new work.")
    async def azkt_get_work_status(request_id: str, cursor: int = 0) -> dict:
        async def run(db, actor, client):
            return await ec.work_status(db, actor, client, request_id, cursor=cursor)
        return await _with_client(run)

    @mcp.tool(name="azkt_reply_to_manager",
              description="Continue one specific mission with the clarification, corrected facts or extra admitted "
                          "asset ids Manager asked for. A reply can never approve a consequential action.")
    async def azkt_reply_to_manager(request_id: str, message: str, asset_ids: list[str] | None = None) -> dict:
        async def run(db, actor, client):
            return await ec.reply(db, actor, client, request_id, message=message, asset_ids=asset_ids or [],
                                  channel="mcp")
        return await _with_client(run)

    @mcp.tool(name="azkt_find_records",
              description="Resolve authorized vehicle, contact and task ids with short identifying context, so a "
                          "caller never has to guess an id and never receives the whole corpus.")
    async def azkt_find_records(q: str, limit: int = 10) -> dict:
        async def run(db, actor, client):
            return await ec.find_records(db, actor, q, limit=limit)
        return await _with_client(run)

    @mcp.tool(name="azkt_prepare_upload",
              description="Create a bounded, expiring upload session for an approved media type and return upload "
                          "instructions. Finalize separately; AZKT validates checksum, type and ownership before "
                          "returning an asset id. AZKT never fetches a URL supplied in a prompt.")
    async def azkt_prepare_upload(content_type: str, size_bytes: int | None = None, filename: str | None = None,
                                  purpose: str = "intake") -> dict:
        async def run(db, actor, client):
            out = await ec.prepare_upload(db, actor, content_type=content_type, size_bytes=size_bytes,
                                          filename=filename, purpose=purpose)
            await db.commit()
            return out
        return await _with_client(run)


# ── ASGI mount ───────────────────────────────────────────────────────────────
def _unauthorized(message: str, status: int = 401) -> tuple[int, dict]:
    return status, {"jsonrpc": "2.0", "error": {"code": -32001, "message": message}, "id": None}


class McpAuthMount:
    """ASGI middleware: authorize, then hand the raw scope to the MCP session manager.

    Intercepting before the router avoids Starlette's Mount trailing-slash redirect (a 307 would lose the
    POST body for some clients) and guarantees no request reaches the session manager unauthenticated.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not (path == MOUNT_PATH or path.startswith(MOUNT_PATH + "/")):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        from .. import db as dbmod
        from ..core.errors import DomainError
        from ..services import external_clients as ec
        try:
            async with dbmod.SessionLocal() as db:
                client, actor = await ec.authenticate(
                    db, headers.get("authorization"),
                    delegation_depth_header=headers.get("x-azkt-delegation-depth"), transport="mcp")
                caller = {"client_id": client.id, "client_name": client.name,
                          "delegation_depth": actor.delegation_depth}
        except DomainError as e:
            await _send_json(send, e.status_code if e.status_code in (401, 403, 422, 429) else 401,
                             {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32001, "message": e.message, "data": e.to_dict()}},
                             extra_headers=[(b"www-authenticate", b'Bearer realm="AZKT"')] if e.status_code == 403 else None)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("MCP auth failed")
            await _send_json(send, 500, {"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32603, "message": f"auth error: {type(e).__name__}"}})
            return
        _CALLER.set(caller)
        try:
            manager = await _ensure_manager()
        except McpUnavailable as e:
            await _send_json(send, 503, {"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32603, "message": str(e)}})
            return
        await manager.handle_request(scope, receive, send)


async def _send_json(send, status: int, body: dict, *, extra_headers: list | None = None) -> None:
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    headers += list(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


async def _ensure_manager():
    """Start the Streamable HTTP session manager lazily, in its own long-lived task.

    The manager needs a running task group. Mounting inside FastAPI means the sub-app's lifespan is not
    run, and the ASGI test client does not run lifespans at all, so the first request starts it here and a
    dedicated task owns it until the process ends.
    """
    import asyncio
    task = _STATE.get("task")
    if task is not None and not task.done():
        await _STATE["ready"].wait()
        return _STATE["manager"]

    from mcp.server.transport_security import TransportSecuritySettings
    hosts, origins = allowed_hosts()
    mcp = server()
    if not hasattr(mcp, "streamable_http_app"):
        raise McpUnavailable("the installed MCP SDK has no streamable_http_app factory")
    # builds and stores the session manager with our security settings; the returned Starlette app is
    # unused because the middleware above owns routing.
    mcp.streamable_http_app(streamable_http_path=MOUNT_PATH, stateless_http=True, json_response=False,
                            transport_security=TransportSecuritySettings(
                                enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins))
    manager = mcp.session_manager
    ready = asyncio.Event()

    async def runner():
        try:
            async with manager.run():
                ready.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:  # pragma: no cover - process shutdown
            raise
        except Exception:  # noqa: BLE001  # pragma: no cover
            log.exception("MCP session manager stopped")
            ready.set()

    _STATE["manager"] = manager
    _STATE["ready"] = ready
    _STATE["task"] = asyncio.create_task(runner())
    await ready.wait()
    return manager


def mount_mcp(app) -> None:
    """Called from `main.create_app()`. Adds the authenticated `/mcp` Streamable HTTP endpoint."""
    _server_class()          # fail fast (and visibly) when the SDK is missing
    app.add_middleware(McpAuthMount)
    _STATE["mounted"] = True
    log.info("MCP Streamable HTTP endpoint mounted at %s", MOUNT_PATH)


def tool_catalog() -> list[dict]:
    """The tool surface, for `/api/integrations/v1/openapi-lite` and the Settings page."""
    return [
        {"name": "azkt_ask_manager", "purpose": "Ask a question or initiate authorized work"},
        {"name": "azkt_get_work_status", "purpose": "Mission progress, result, evidence and review links (cursor)"},
        {"name": "azkt_reply_to_manager", "purpose": "Continue a specific mission with clarification or assets"},
        {"name": "azkt_find_records", "purpose": "Resolve authorized vehicle/contact/task ids with context"},
        {"name": "azkt_prepare_upload", "purpose": "Bounded expiring upload session for approved media types"},
    ]
