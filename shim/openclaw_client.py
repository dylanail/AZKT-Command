"""Driver for one OpenClaw agent on this droplet.

Grounded in discovery + OpenClaw docs:
  - Worker agents run via systemd --user timers (heartbeat is globally
    disabled) -> health/state come from systemd, not OpenClaw heartbeat.
  - Chat/run go through the OpenClaw Gateway OpenAI-compatible API at
    127.0.0.1:18789; the agent is addressed via the `model` field = agent_id;
    auth is `Authorization: Bearer <gateway token>`.
  - One-off run = start the systemd --user service the timer triggers.
  - Pause/resume = stop/start the timer (reversible; not disable).

Anything build-specific (gateway chat path, agent-select field) is overridable
via env so a wrong assumption is a one-line config fix, not a code change.
"""
from __future__ import annotations

import os
import subprocess

import httpx

GATEWAY = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
CLI = os.environ.get("OPENCLAW_CLI", "openclaw")
CHAT_PATH = os.environ.get("OPENCLAW_CHAT_PATH", "/v1/chat/completions")


def _systemctl_user(*args: str, timeout: int = 15) -> tuple[int, str, str]:
    p = subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()


class OpenClawClient:
    def __init__(self, agent_id: str, model: str, timer_unit: str = "", service_unit: str = ""):
        self.agent_id = agent_id
        self.model = model
        self.timer_unit = timer_unit
        self.service_unit = service_unit

    # ── systemd timer health (the silent-failure guard) ────────────────────
    def timer_health(self) -> dict:
        if not self.timer_unit:
            return {"timer": None, "managed": False}  # e.g. main (DM, no timer)
        rc, out, _ = _systemctl_user(
            "show", self.timer_unit,
            "-p", "ActiveState", "-p", "SubState",
            "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime",
            "-p", "UnitFileState",
        )
        props = dict(
            line.split("=", 1) for line in out.splitlines() if "=" in line
        )
        active = props.get("ActiveState") == "active"
        svc_failed = None
        if self.service_unit:
            src, sout, _ = _systemctl_user("is-failed", self.service_unit)
            svc_failed = sout == "failed"
        return {
            "timer": self.timer_unit,
            "managed": True,
            "active": active,
            "unit_file_state": props.get("UnitFileState"),
            "last_trigger_usec": props.get("LastTriggerUSec"),
            "next_elapse_usec": props.get("NextElapseUSecRealtime"),
            "service_failed": svc_failed,
            # Degraded == the inbox-loop-dead-since-May-5 class of bug.
            "degraded": (not active) or bool(svc_failed),
        }

    def cron_jobs(self) -> dict:
        """The two OpenClaw cron jobs (read-only context)."""
        try:
            p = subprocess.run([CLI, "cron", "list", "--json"],
                               capture_output=True, text=True, timeout=20)
            return {"rc": p.returncode, "raw": p.stdout.strip()[:4000],
                    "stderr": p.stderr.strip() or None}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    # ── lifecycle ──────────────────────────────────────────────────────────
    def run_once(self) -> dict:
        if not self.service_unit:
            return {"ok": False, "error": "agent has no systemd service (e.g. main)"}
        rc, out, err = _systemctl_user("start", self.service_unit, timeout=30)
        return {"ok": rc == 0, "stdout": out, "stderr": err}

    def set_timer_enabled(self, enabled: bool) -> dict:
        if not self.timer_unit:
            return {"ok": False, "error": "no timer for this agent"}
        rc, out, err = _systemctl_user("start" if enabled else "stop", self.timer_unit)
        return {"ok": rc == 0, "stdout": out, "stderr": err}

    # ── chat via Gateway OpenAI-compatible API ─────────────────────────────
    async def chat(self, message: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if GATEWAY_TOKEN:
            headers["Authorization"] = f"Bearer {GATEWAY_TOKEN}"
        body = {
            "model": self.agent_id,  # OpenClaw addresses the agent by model id
            "messages": [{"role": "user", "content": message}],
        }
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{GATEWAY}{CHAT_PATH}", headers=headers, json=body)
        data = _safe_json(r)
        reply, usage = "", {}
        if isinstance(data, dict):
            try:
                reply = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                reply = ""
            usage = data.get("usage", {}) or {}
        return {"ok": r.status_code == 200, "status_code": r.status_code,
                "reply": reply, "usage": usage, "raw": data if not reply else None}

    async def gateway_health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{GATEWAY}/v1/models",
                                headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"}
                                if GATEWAY_TOKEN else {})
            return {"reachable": r.status_code < 500, "status_code": r.status_code}
        except Exception as e:  # noqa: BLE001
            return {"reachable": False, "error": str(e)}


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return r.text[:1000]
