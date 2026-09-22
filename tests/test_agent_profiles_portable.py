"""Focused regressions without Postgres. Full integration coverage remains in backend/tests."""
import asyncio
import io
import zipfile

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend.app.db import Base
from backend.app.domain.actors import Actor
from backend.app.domain.commands import CommandContext, dispatch
from backend.app.domain.policy import effective_perms
from backend.app.models.agent_profiles import AgentProfile, AgentProfileVersion
from backend.app.models.runtime import ActivityEntry, Approval, CommandLog, Event, Mission, Permission, WorkflowControl, Run, RunStep
from backend.app.models.knowledge import Procedure, ProcedureVersion
from backend.app.services import agent_profiles as svc
from backend.app.agent import runtime


def test_import_removes_tokens_and_excludes_scripts():
    data = io.BytesIO()
    token = "sk-abcdefghijklmnop"
    with zipfile.ZipFile(data, "w") as z:
        z.writestr("bundle/workspace-scout/IDENTITY.md", "Find suitable auction candidates.")
        z.writestr("bundle/workspace-scout/TOOLS.md", f"Token {token}\nAuthorization: Bearer abc123")
        z.writestr("bundle/workspace-scout/scripts/private.py", "DO_NOT_IMPORT")
    parsed = svc.inspect_openclaw_bundle(data.getvalue())
    result = svc.imported_payload("sourcing", "scout", parsed, "test.zip")
    assert token not in str(result) and "abc123" not in str(result)
    assert "DO_NOT_IMPORT" not in str(result)
    assert "REDACTED" in result["tool_guidance"]


def test_rules_are_rejected_not_silently_truncated():
    with pytest.raises(ValueError):
        svc.ProfileProposeIn(role="sourcing", mission="Find trucks", core_rules=["x" * 2001])


def test_version_lifecycle_manager_review_and_pinned_context():
    async def check():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        tables = [AgentProfile, AgentProfileVersion, ActivityEntry, Approval, CommandLog, Event,
                  Mission, Permission, WorkflowControl, Procedure, ProcedureVersion, Run, RunStep]
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[t.__table__ for t in tables]))
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            owner = Actor(user_id="owner-test", role="owner", scope="all", perms=effective_perms("owner", {}))
            manager = Actor(kind="agent", user_id="owner-test", role="owner", agent_role="manager",
                            scope="all", perms=owner.perms)
            async def call(name, payload, actor=owner):
                return await dispatch(CommandContext(db=db, actor=actor), "agent_profiles." + name, payload)
            content = {"role": "sourcing", "mission": "Find suitable auction candidates.",
                       "operating_instructions": "x" * 17000 + "END_OF_INSTRUCTIONS"}
            first = (await call("propose", content)).data["version"]
            ref = {"version_id": first["id"]}
            await call("validate", ref)
            await call("activate", ref)
            await call("validate", ref)
            assert (await svc.current_version_for(db, "sourcing")).id == first["id"]
            mission = await runtime.create_mission(db, manager, outcome="Find trucks", role="sourcing")
            assert mission.agent_profile_version == first["id"]
            assert "END_OF_INSTRUCTIONS" in await svc.version_text(db, first["id"])
            second = (await call("propose", {**content, "mission": "Find only verified candidates."}, manager)).data["version"]
            assert second["source_kind"] == "manager"
            second_ref = {"version_id": second["id"]}
            await call("validate", second_ref, manager)
            approval = await call("activate", second_ref, manager)
            assert approval.status == "needs_review"
            assert (await svc.current_version_for(db, "sourcing")).id == first["id"]
            await call("activate", second_ref)
            await call("validate", ref)
            assert (await db.get(AgentProfileVersion, first["id"])).stage == "superseded"
            assert mission.agent_profile_version == first["id"]
            assert "Find suitable" in await runtime._agent_profile_text(db, mission)
            assert len((await svc.get_profile(db, "sourcing"))["versions"]) == 2
            from backend.app.routers.agent import get_run
            from backend.app.core.errors import NotFound
            run = Run(mission_id=mission.id, status="succeeded")
            db.add(run)
            await db.flush()
            db.add(RunStep(run_id=run.id, seq=1, tool_name="agent_profiles.get", decision="allowed",
                           output={"evidence": "recorded"}))
            await db.flush()
            snapshot = await get_run(run.id, owner, db)
            assert snapshot["instruction_version"]["id"] == first["id"]
            assert snapshot["updates"] and snapshot["steps"][0]["decision"] == "allowed"
            with pytest.raises(NotFound):
                await get_run(run.id, Actor(user_id="unrelated", role="manager", scope="assigned"), db)
        await engine.dispose()
    asyncio.run(check())
