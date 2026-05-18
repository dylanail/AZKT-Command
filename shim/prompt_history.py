"""Git-backed version history for an agent's system prompt (SOUL.md).

OpenClaw loads SOUL.md from the agent workspace every session. We keep a
dedicated git repo alongside it so every edit is a commit the dashboard can
diff and roll back to — without touching the agent's own workspace VCS.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class PromptHistory:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).expanduser()
        self.soul = self.workspace / "SOUL.md"
        self.git_dir = self.workspace / ".azkt-prompt-history"

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.workspace}", *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def ensure_repo(self) -> None:
        if not self.git_dir.exists():
            subprocess.run(["git", "init", "--bare", "-q", str(self.git_dir)], check=True)
            if self.soul.exists():
                self.commit("baseline: existing SOUL.md")

    def read(self) -> str:
        return self.soul.read_text(encoding="utf-8") if self.soul.exists() else ""

    def commit(self, message: str) -> str:
        self._git("add", "SOUL.md")
        try:
            self._git(
                "-c", "user.email=dashboard@azkt.local",
                "-c", "user.name=AZKT Command",
                "commit", "-q", "-m", message,
            )
        except subprocess.CalledProcessError:
            return ""  # nothing changed
        return self._git("rev-parse", "HEAD")

    def write(self, content: str, message: str) -> str:
        self.ensure_repo()
        self.soul.write_text(content, encoding="utf-8")
        return self.commit(message)

    def history(self, limit: int = 50) -> list[dict]:
        if not self.git_dir.exists():
            return []
        out = self._git("log", f"-{limit}", "--pretty=format:%H%x1f%ct%x1f%s")
        rows = []
        for line in filter(None, out.splitlines()):
            h, ts, msg = line.split("\x1f")
            rows.append({"sha": h, "ts": int(ts), "message": msg})
        return rows

    def at(self, sha: str) -> str:
        return self._git("show", f"{sha}:SOUL.md")

    def rollback(self, sha: str) -> str:
        prior = self.at(sha)
        return self.write(prior, f"rollback to {sha[:8]}")
