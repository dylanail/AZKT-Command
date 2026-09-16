"""Serve the built dashboard and client-side routes from the web container."""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse


def mount_frontend(app: FastAPI, dist: Path) -> None:
    root = dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        # Never disguise missing backend endpoints as successful HTML responses.
        if path.split("/", 1)[0] in {"api", "auth", "mcp", "healthz", "readyz"}:
            raise HTTPException(404)
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(404)
        if candidate.is_file():
            return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
        if path.startswith("assets/") or Path(path).suffix:
            raise HTTPException(404)
        index = root / "index.html"
        if not index.is_file():
            return RedirectResponse("/enroll")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
