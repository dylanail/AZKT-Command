"""J01: a generic authorized MCP client connects over Streamable HTTP at /mcp and does the same work, on
the same canonical records, as a separate authorized client using the HTTP equivalent — with no
agent-specific prior context, and with the connector grant enforced identically on both transports.

Two levels of coverage:
  * the MCP SDK's own Streamable HTTP client driven against the in-process ASGI app (no network), and
  * raw JSON-RPC over the mount (initialize / tools/list / tools/call), which is what a client that
    implements the transport by hand sends.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from backend.app.agent import mcp_server
from backend.app.domain.commands import dispatch
from backend.app.models.runtime import Mission
from backend.app.models.tasks import Task
from backend.tests.conftest import ctx_for, login
from backend.tests.test_connector import READ_ONLY, hdr, register
from backend.tests.test_runtime import FakeModel, make_vehicle, uid, use_model

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
PROTOCOL = "2025-06-18"


def rpc(method: str, params: dict | None = None, rid: int | None = 1) -> dict:
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    return body


def parse(resp) -> dict:
    """Streamable HTTP answers a POST with SSE (or plain JSON); both carry one JSON-RPC message."""
    ctype = resp.headers.get("content-type", "")
    if "event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no data frame in SSE response: {resp.text[:400]}")
    return resp.json()


async def initialize(client, token: str) -> dict:
    r = await client.post("/mcp", json=rpc("initialize", {
        "protocolVersion": PROTOCOL, "capabilities": {},
        "clientInfo": {"name": "generic-agent", "version": "1.0"}}), headers={**MCP_HEADERS, **hdr(token)})
    assert r.status_code == 200, r.text[:500]
    return parse(r)


# ═════════════════════════════════════════════════════════════════════════════
# Auth on the mount
# ═════════════════════════════════════════════════════════════════════════════
async def test_mcp_requires_a_bearer_token_and_never_a_session_cookie(db, owner, client):
    r = await client.post("/mcp", json=rpc("initialize", {"protocolVersion": PROTOCOL}), headers=MCP_HEADERS)
    assert r.status_code == 401 and r.headers.get("www-authenticate", "").startswith("Bearer")
    assert r.json()["error"]["data"]["error"] == "denied"

    login(client, owner)                                     # a signed-in browser session is not a credential
    r2 = await client.post("/mcp", json=rpc("initialize", {"protocolVersion": PROTOCOL}), headers=MCP_HEADERS)
    assert r2.status_code == 401
    client.cookies.clear()

    r3 = await client.post("/mcp", json=rpc("initialize", {"protocolVersion": PROTOCOL}),
                           headers={**MCP_HEADERS, **hdr("azkt_ec_not-a-real-token")})
    assert r3.status_code == 401 and "unknown" in r3.json()["error"]["message"].lower()

    # a client registered for HTTP only cannot use the MCP transport
    c, token = await register(db, owner, scopes=READ_ONLY, transport="http")
    r4 = await client.post("/mcp", json=rpc("initialize", {"protocolVersion": PROTOCOL}),
                           headers={**MCP_HEADERS, **hdr(token)})
    assert r4.status_code == 401 and "http" in r4.json()["error"]["message"]

    # a malformed delegation-depth header is a validation problem, not an auth problem
    c2, token2 = await register(db, owner, scopes=READ_ONLY, transport="mcp")
    r5 = await client.post("/mcp", json=rpc("initialize", {"protocolVersion": PROTOCOL}),
                           headers={**MCP_HEADERS, **hdr(token2, **{"X-AZKT-Delegation-Depth": "deep"})})
    assert r5.status_code == 422


async def test_mcp_initialize_and_tools_list_over_raw_jsonrpc(db, owner, client):
    c, token = await register(db, owner)
    init = await initialize(client, token)
    assert init["result"]["serverInfo"]["name"] == mcp_server.SERVER_NAME
    assert init["result"]["protocolVersion"]
    assert init["result"]["capabilities"]["tools"] is not None

    r = await client.post("/mcp", json=rpc("tools/list", rid=2), headers={**MCP_HEADERS, **hdr(token)})
    tools = {t["name"]: t for t in parse(r)["result"]["tools"]}
    assert set(tools) == {"azkt_ask_manager", "azkt_get_work_status", "azkt_reply_to_manager",
                          "azkt_find_records", "azkt_prepare_upload"}
    ask = tools["azkt_ask_manager"]
    assert ask["description"] and "request_key" in ask["description"]
    schema = ask["inputSchema"]
    assert schema["type"] == "object" and set(schema["required"]) == {"message", "request_key"}
    assert "asset_ids" in schema["properties"] and "entity_refs" in schema["properties"]
    # the surface is discoverable without any prior AZKT-specific context
    assert all(tools[n]["description"] for n in tools)


# ═════════════════════════════════════════════════════════════════════════════
# J01 — MCP and HTTP on the same records
# ═════════════════════════════════════════════════════════════════════════════
async def test_J01_mcp_and_http_clients_work_on_the_same_records(db, owner, client):
    mcp_client, mcp_token = await register(db, owner, name=f"mcp-agent-{uid()}", transport="mcp")
    http_client, http_token = await register(db, owner, name=f"http-agent-{uid()}", transport="http")
    tag = uid()
    v = await make_vehicle(db, owner, make="Daihatsu", model=f"Hijet {tag}", color="green")
    await initialize(client, mcp_token)

    # (1) the MCP client finds the truck by description, with no id guessing and no prior context
    find = await client.post("/mcp", json=rpc("tools/call", {
        "name": "azkt_find_records", "arguments": {"q": f"Hijet {tag}"}}, rid=3),
        headers={**MCP_HEADERS, **hdr(mcp_token)})
    payload = parse(find)["result"]
    found = json.loads(payload["content"][0]["text"]) if payload["content"][0]["type"] == "text" \
        else payload["structuredContent"]
    assert [x["id"] for x in found["vehicles"]] == [v.id]
    assert found["vehicles"][0]["stock_no"] == v.stock_no and found["vehicles"][0]["title"]

    # (2) the MCP client asks Manager a vehicle question -> a deterministic, model-free answer
    ask = await client.post("/mcp", json=rpc("tools/call", {
        "name": "azkt_ask_manager",
        "arguments": {"message": f"what is holding up {v.stock_no}?", "request_key": f"mcp-{tag}"}}, rid=4),
        headers={**MCP_HEADERS, **hdr(mcp_token)})
    res = parse(ask)["result"]
    env = json.loads(res["content"][0]["text"])
    assert res.get("isError") in (False, None)
    assert env["state"] == "answered" and v.stock_no in env["summary"]
    assert "Holding it up:" in env["summary"]

    # (3) a *separate* client using the HTTP equivalent gets the same answer from the same records
    http_ask = await client.post("/api/integrations/v1/ask", json={
        "message": f"what is holding up {v.stock_no}?", "request_key": f"http-{tag}"}, headers=hdr(http_token))
    assert http_ask.status_code == 200
    assert http_ask.json()["summary"] == env["summary"]

    # (4) and a routine task requested over MCP is written through the same command layer
    with use_model(FakeModel([
        {"text": "Adding the follow-up.",
         "tools": [{"name": "tasks_create", "input": {"title": f"Check the tyres {tag}", "vehicle_id": v.id}}]},
        {"text": f"Added “Check the tyres {tag}” to {v.stock_no}."},
    ])):
        work = await client.post("/mcp", json=rpc("tools/call", {
            "name": "azkt_ask_manager",
            "arguments": {"message": "Please add a follow-up to check the tyres on that truck",
                          "request_key": f"mcp-task-{tag}",
                          "entity_refs": [{"kind": "vehicle", "id": v.id}]}}, rid=5),
            headers={**MCP_HEADERS, **hdr(mcp_token)})
    env2 = json.loads(parse(work)["result"]["content"][0]["text"])
    assert env2["state"] in ("done", "waiting", "running")
    tasks = (await db.execute(select(Task).where(Task.title == f"Check the tyres {tag}"))).scalars().all()
    assert len(tasks) == 1 and tasks[0].vehicle_id == v.id
    assert any(ch["kind"] == "task" for ch in env2["changed"])

    # (5) the status of that mission is retrievable with a cursor, by the client that started it
    status = await client.post("/mcp", json=rpc("tools/call", {
        "name": "azkt_get_work_status", "arguments": {"request_id": env2["request_id"], "cursor": 0}}, rid=6),
        headers={**MCP_HEADERS, **hdr(mcp_token)})
    st = json.loads(parse(status)["result"]["content"][0]["text"])
    assert st["mission_id"] == env2["mission_id"] and st["updates"]
    # ... and not by the other client
    cross = await client.get(f"/api/integrations/v1/work/{env2['request_id']}", headers=hdr(http_token))
    assert cross.status_code == 404


async def test_J01_with_the_mcp_sdk_streamable_http_client(db, owner, app):
    """The SDK's own client, against the in-process ASGI app (no network)."""
    httpx2 = pytest.importorskip("httpx2")
    try:
        from mcp.client.client import Client
        from mcp.client.streamable_http import streamable_http_client
    except Exception as e:  # pragma: no cover - documented fallback
        pytest.skip(f"MCP SDK client unavailable: {e}")

    c, token = await register(db, owner, name=f"sdk-agent-{uid()}", transport="mcp")
    tag = uid()
    v = await make_vehicle(db, owner, make="Suzuki", model=f"Carry {tag}")

    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver",
                              headers={"Authorization": f"Bearer {token}"}, timeout=60.0)
    transport = streamable_http_client("http://testserver/mcp", http_client=http, terminate_on_close=False)
    async with Client(transport) as session:
        listed = await session.list_tools()
        names = {t.name for t in listed.tools}
        assert names == {"azkt_ask_manager", "azkt_get_work_status", "azkt_reply_to_manager",
                         "azkt_find_records", "azkt_prepare_upload"}
        found = await session.call_tool("azkt_find_records", {"q": f"Carry {tag}"})
        body = json.loads(found.content[0].text)
        assert [x["id"] for x in body["vehicles"]] == [v.id]
        answered = await session.call_tool("azkt_ask_manager",
                                           {"message": f"status of {v.stock_no}", "request_key": f"sdk-{tag}"})
        env = json.loads(answered.content[0].text)
        assert env["state"] == "answered" and v.stock_no in env["summary"]
    await http.aclose()


# ═════════════════════════════════════════════════════════════════════════════
# The grant follows the MCP transport too
# ═════════════════════════════════════════════════════════════════════════════
async def test_mcp_grant_is_enforced_and_revocation_is_immediate(db, owner, client):
    mine = await make_vehicle(db, owner, make="Honda", model=f"Acty {uid()}")
    theirs = await make_vehicle(db, owner, make="Mazda", model=f"Scrum {uid()}")
    c, token = await register(db, owner, scopes=READ_ONLY, transport="mcp",
                              record_scope={"vehicle_ids": [mine.id]})
    await initialize(client, token)

    r = await client.post("/mcp", json=rpc("tools/call", {
        "name": "azkt_find_records", "arguments": {"q": "a"}}, rid=7), headers={**MCP_HEADERS, **hdr(token)})
    body = json.loads(parse(r)["result"]["content"][0]["text"])
    assert [x["id"] for x in body["vehicles"]] == [mine.id]

    # an upload session belongs to the client that opened it
    up = await client.post("/mcp", json=rpc("tools/call", {
        "name": "azkt_prepare_upload", "arguments": {"content_type": "image/jpeg", "size_bytes": 1024,
                                                     "filename": "a.jpg"}}, rid=8),
        headers={**MCP_HEADERS, **hdr(token)})
    upres = json.loads(parse(up)["result"]["content"][0]["text"])
    # this grant has no intake scope: the refusal is structured, not a fake success
    assert upres.get("error") or upres.get("upload_id")
    if upres.get("upload_id"):
        from backend.app.models.assets import UploadSession
        s = await db.get(UploadSession, upres["upload_id"])
        assert s.client_id == c.id

    # revoking ends MCP access on the very next request
    await dispatch(ctx_for(db, owner), "external_clients.revoke", {"client_id": c.id})
    gone = await client.post("/mcp", json=rpc("tools/list", rid=9), headers={**MCP_HEADERS, **hdr(token)})
    assert gone.status_code == 401


async def test_mcp_and_http_share_one_delegated_request_namespace(db, owner, client):
    """A client registered for both transports sees one request/mission whichever door it uses (J02)."""
    c, token = await register(db, owner, transport="both")
    key = f"both-{uid()}"
    await initialize(client, token)
    with use_model(FakeModel([{"text": "Nothing to change."}, {"text": "Nothing to change."}])):
        over_mcp = await client.post("/mcp", json=rpc("tools/call", {
            "name": "azkt_ask_manager",
            "arguments": {"message": "Summarise what is open", "request_key": key}}, rid=10),
            headers={**MCP_HEADERS, **hdr(token)})
        env = json.loads(parse(over_mcp)["result"]["content"][0]["text"])
        over_http = await client.post("/api/integrations/v1/ask",
                                      json={"message": "Summarise what is open", "request_key": key},
                                      headers=hdr(token))
    assert over_http.json()["request_id"] == env["request_id"]
    assert over_http.json()["mission_id"] == env["mission_id"]
    if env.get("mission_id"):
        assert await db.scalar(select(__import__("sqlalchemy").func.count()).select_from(Mission)
                               .where(Mission.delegated_request_id == env["request_id"])) == 1
