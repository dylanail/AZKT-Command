"""Translation document access (spec §8.2 steps 6–7). Google Docs revision polling lives behind this
interface: `get_document(doc_ref) -> {revision, text, modified_at}`. The live implementation returns
`unsupported` until Google is connected (stage 2 wires OAuth); the fixture implementation serves tests.

`check_completeness(text, required_sections)` decides completeness deterministically from content:
every required section must appear as a heading AND have non-empty content beneath it. A stable
edit timestamp alone never counts as completion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from ..core.errors import Unsupported

# Exporter completion convention: sections the translator fills before the doc counts as complete.
DEFAULT_REQUIRED_SECTIONS = ["Auction sheet", "Grade", "Condition", "Translator notes", "Translation complete"]
# Accepted heading aliases (English + Japanese labels the exporter uses)
SECTION_ALIASES = {
    "auction sheet": ["auction sheet", "オークションシート", "出品票"],
    "grade": ["grade", "評価点", "評価"],
    "condition": ["condition", "状態", "コンディション"],
    "translator notes": ["translator notes", "notes", "備考", "翻訳者メモ"],
    "translation complete": ["translation complete", "complete", "完了", "翻訳完了", "done"],
}


@dataclass
class DocResult:
    status: str                    # ok | unsupported | error
    revision: str | None = None
    text: str = ""
    modified_at: str | None = None
    reason: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"status": self.status, "revision": self.revision, "modified_at": self.modified_at, "reason": self.reason,
                "text_length": len(self.text or "")}


class TranslationDocs:
    async def get_document(self, doc_ref: str) -> DocResult:  # pragma: no cover - interface
        raise NotImplementedError


class FixtureTranslationDocs(TranslationDocs):
    def __init__(self, docs: dict[str, dict] | None = None):
        self.docs = docs or {}

    def put(self, doc_ref: str, revision: str, text: str, modified_at: str | None = None) -> None:
        self.docs[doc_ref] = {"revision": revision, "text": text, "modified_at": modified_at}

    async def get_document(self, doc_ref: str) -> DocResult:
        d = self.docs.get(doc_ref)
        if d is None:
            return DocResult("error", reason={"kind": "not_found", "detail": f"no fixture doc {doc_ref}"})
        return DocResult("ok", revision=str(d.get("revision")), text=d.get("text") or "", modified_at=d.get("modified_at"))


class GoogleDocsTranslationDocs(TranslationDocs):
    """Live Google Docs access. Unsupported until a Google connection exists (stage 2)."""

    def __init__(self, connection=None):
        self.connection = connection

    async def get_document(self, doc_ref: str) -> DocResult:
        if self.connection is None or getattr(self.connection, "status", None) != "connected":
            return DocResult("unsupported", reason={"kind": "not_configured",
                                                    "detail": "Google Docs is not connected; translation polling is unavailable"})
        return DocResult("unsupported", reason={"kind": "not_implemented",
                                                "detail": "Google Docs revision polling is wired in stage 2"})


def _heading_lines(text: str) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate((text or "").splitlines()):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(?:#+\s*|\[|【)?\s*([^\]】:：#]{2,60})\s*(?:\]|】)?\s*[:：]?\s*$", s)
        if m:
            out.append((i, m.group(1).strip().lower()))
    return out


def _section_present(text: str, label: str) -> tuple[bool, bool]:
    """(heading found, has content beneath the heading before the next heading)."""
    lines = (text or "").splitlines()
    headings = _heading_lines(text)
    aliases = SECTION_ALIASES.get(label.lower(), [label.lower()])
    for idx, (line_no, htxt) in enumerate(headings):
        if any(htxt == a or htxt.startswith(a) for a in aliases):
            end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
            body = [ln for ln in lines[line_no + 1:end] if ln.strip()]
            if body:
                return True, True
            # a "complete" marker is allowed as a bare heading-line with a value on the same line
            if label.lower() == "translation complete":
                return True, True
            return True, False
    # inline form: "Grade: 4" on one line
    for ln in lines:
        s = ln.strip().lower()
        for a in aliases:
            if s.startswith(a + ":") or s.startswith(a + "："):
                val = s.split(":", 1)[-1].split("：", 1)[-1].strip()
                return True, bool(val)
    return False, False


def check_completeness(text: str, required_sections: list[str] | None = None) -> dict:
    """Deterministic completeness: {ok, present: [...], missing: [...], empty: [...], sections: {label: state}}."""
    required = list(required_sections or DEFAULT_REQUIRED_SECTIONS)
    present, missing, empty, sections = [], [], [], {}
    for label in required:
        found, has_content = _section_present(text or "", label)
        if not found:
            missing.append(label)
            sections[label] = "missing"
        elif not has_content:
            empty.append(label)
            sections[label] = "empty"
        else:
            present.append(label)
            sections[label] = "present"
    return {"ok": not missing and not empty, "present": present, "missing": missing, "empty": empty,
            "sections": sections, "required": required, "checked_at": datetime.now().astimezone().isoformat()}


def identity_matches(text: str, lot_no: str | None, auction_house: str | None = None) -> dict:
    """The doc must name the exact lot (and, when present, the house) so a translation of another
    vehicle cannot be attached (spec §8.2 step 7)."""
    body = (text or "").lower()
    lot_ok = bool(lot_no) and str(lot_no).lower() in body
    house_ok = (not auction_house) or auction_house.lower() in body
    return {"ok": lot_ok and house_ok, "expected_lot": lot_no, "lot_found": lot_ok,
            "expected_house": auction_house, "house_found": house_ok}


def docs_for(spec: dict | None, connection=None) -> TranslationDocs:
    spec = spec or {}
    if spec.get("kind") == "fixture":
        return FixtureTranslationDocs(spec.get("docs"))
    if spec.get("kind") in (None, "google_docs"):
        return GoogleDocsTranslationDocs(connection)
    raise Unsupported(f"unknown translation doc source {spec.get('kind')!r}")
