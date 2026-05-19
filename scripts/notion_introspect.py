"""Introspect the three Notion databases and emit a DRAFT schema mapping.

Run on the droplet (needs NOTION_TOKEN + the three DB IDs in .env):

    PYTHONPATH=. python scripts/notion_introspect.py

It prints every property name + type for VEHICLES, CUSTOMERS, IRQS, and
writes config/notion_schema.draft.json with best-guess slot matches. NOTHING
is used until you review the draft, fix any wrong/blank matches against the
printed property list, and copy it over config/notion_schema.json. The draft
is a human-confirmation aid, not an auto-applied guess.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env minimally (no extra deps).
ENV = ROOT / ".env"
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ.get("NOTION_TOKEN", "")
DBS = {
    "VEHICLES": os.environ.get("NOTION_VEHICLES_DB_ID", ""),
    "CUSTOMERS": os.environ.get("NOTION_CUSTOMERS_DB_ID", ""),
    "IRQS": os.environ.get("NOTION_IRQS_DB_ID", ""),
}
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28"}

# Heuristic hints only — to pre-fill the draft for you to confirm.
HINTS = {
    "title": ["name", "title", "vehicle", "vin"],
    "sold_price_usd": ["sold price"],
    "landed_cost_usd": ["landed cost"],
    "won_date": ["won date", "auction won"],
    "sold_date": ["sold date"],
    "auction_url": ["auction url", "listing url", "url", "link"],
    "days_on_market": ["days on market", "dom"],
    "email": ["email", "e-mail"],
    "phone": ["phone", "mobile"],
    "status": ["status", "state"],
    "created_at": ["created", "received", "date received"],
    "assignee": ["assignee", "owner", "assigned"],
}


def guess(slot: str, names: list[str]) -> str:
    for n in names:
        low = n.lower()
        for h in HINTS.get(slot, []):
            if h in low:
                return n
    return ""


def main() -> None:
    if not TOKEN or not all(DBS.values()):
        print("Missing NOTION_TOKEN or one of NOTION_*_DB_ID in .env")
        sys.exit(1)

    draft: dict = {}
    with httpx.Client(timeout=30) as c:
        for label, db_id in DBS.items():
            r = c.get(f"https://api.notion.com/v1/databases/{db_id}", headers=HEADERS)
            if r.status_code != 200:
                print(f"[{label}] ERROR {r.status_code}: {r.text[:300]}")
                continue
            props = r.json().get("properties", {})
            print(f"\n=== {label} ({len(props)} properties) ===")
            names = []
            for name, meta in sorted(props.items()):
                names.append(name)
                print(f"  {meta.get('type'):14}  {name!r}")
            # Pre-fill obvious slots; leave the rest blank for you.
            common = {k: guess(k, names) for k in
                      ("title", "email", "phone", "status", "created_at",
                       "assignee", "sold_price_usd", "landed_cost_usd",
                       "won_date", "sold_date", "auction_url", "days_on_market")}
            draft[label] = {"_all_properties": names,
                            "_guessed": {k: v for k, v in common.items() if v}}

    out = ROOT / "config" / "notion_schema.draft.json"
    out.write_text(json.dumps(draft, indent=2))
    print(f"\nWrote {out}")
    print("Review it, correct anything wrong using the property list above,")
    print("then transcribe into config/notion_schema.json (exact names).")


if __name__ == "__main__":
    main()
