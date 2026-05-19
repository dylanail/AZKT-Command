from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.passkey import current_user
from ..db import get_db
from ..services import chat_history

router = APIRouter(prefix="/api/chat", tags=["chat"],
                   dependencies=[Depends(current_user)])


@router.get("/history")
async def get_history(agent: str = "all", source: str = "all",
                      limit: int = 200, db: AsyncSession = Depends(get_db)):
    limit = max(1, min(limit, 1000))
    if source not in ("all", "dashboard", "transcript"):
        source = "all"
    return await chat_history.history(db, agent=agent, limit=limit, source=source)
