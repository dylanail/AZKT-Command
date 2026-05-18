"""Thin driver over the OpenClaw CLI + loopback Gateway.

Confirmed from OpenClaw docs:
  - Gateway/Control API binds loopback at http://127.0.0.1:18789
  - CLI: `openclaw cron status|list`, `openclaw cron runs --id <jobId> --limit N`,
    `openclaw system heartbeat last`, `openclaw logs`
  - Heartbeat/cron skip + disabled states are detectable (silent-failure guard)

The exact `run`/`chat` invocation differs by build, so those commands are
templated in config (run_cmd / chat_cmd) instead of hardcoded/guessed.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess

import httpx

GATEWAY = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
CLI = os.environ.get("OPENCLAW_CLI", "openclaw")


class OpenClawClient:
    def __init__(self, agent_id: str, run_cmd: str | None = None, chat_cmd: str | None = None):
        self.agent_id = agent_id
        # {agent} and {message} are substituted; quoted via shlex.
        self.run_cmd = run_cmd or f"{CLI} agent run --id {{agent}} --once"
        self.chat_cmd = chat_cmd or f"{CLI} agent message --id {{agent}} --text {{message}}"

    def _cli(self, *args: str, timeout: int = 30) -> tuple[int, str, str]:
        p = subprocess.run([CLI, *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()

    def cron_status(self) -> dict:
        """Cron/heartbeat health for THIS agent. Powers the silent-failure guard."""
        rc, out, err = self._cli("cron", "status", "--json")
        scheduler_disabled = "scheduler disabled" in (out + err).lower()
        data: dict = {}
        try:
            data = json.loads(out) if out.startswith(("{", "[")) else {"raw": out}
        except json.JSONDecodeError:
            data = {"raw": out}
        return {
            "scheduler_disabled": scheduler_disabled,
            "cli_rc": rc,
            "detail": data,
            "stderr": err or None,
        }

    def heartbeat_last(self) -> dict:
        rc, out, err = self._cli("system", "heartbeat", "last", "--json")
        try:
            return json.loads(out) if out.startswith(("{", "[")) else {"raw": out, "rc": rc}
        except json.JSONDecodeError:
            return {"raw": out, "rc": rc, "stderr": err or None}

    def cron_runs(self, job_id: str, limit: int = 20) -> dict:
        rc, out, err = self._cli("cron", "runs", "--id", job_id, "--limit", str(limit), "--json")
        try:
            return json.loads(out) if out.startswith(("{", "[")) else {"raw": out}
        except json.JSONDecodeError:
            return {"raw": out, "stderr": err or None}

    def set_schedule_enabled(self, enabled: bool) -> dict:
        verb = "enable" if enabled else "disable"
        rc, out, err = self._cli("cron", verb, "--id", self.agent_id)
        return {"ok": rc == 0, "out": out, "err": err}

    def run_once(self, timeout: int = 600) -> dict:
        cmd = shlex.split(self.run_cmd.format(agent=shlex.quote(self.agent_id)))
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "stdout": p.stdout, "stderr": p.stderr}

    def chat(self, message: str, timeout: int = 300) -> dict:
        cmd = shlex.split(
            self.chat_cmd.format(agent=shlex.quote(self.agent_id), message=shlex.quote(message))
        )
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "reply": p.stdout.strip(), "stderr": p.stderr}

    async def gateway_health(self) -> dict:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{GATEWAY}/health")
            return {"status_code": r.status_code, "body": _safe_json(r)}


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return r.text[:500]
