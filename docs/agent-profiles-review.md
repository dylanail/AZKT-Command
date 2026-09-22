# Agent instructions: review handoff

Status: user approved committing and merging into GitHub `main`. Outstanding verification below still applies. Deployment health is not verified by the merge.

## What is implemented

- Owner: Agents → select role → Instructions. Edit mission, voice, rules, playbook, reporting, escalation, tool guidance and schedule guidance directly.
- Save creates an immutable draft. Open any saved/imported version, compare it with live content, edit into a new version, check and activate. History includes source, author, warnings and activation details.
- Manager: registered read/propose/validate/activate tools use the existing command and permission system. The owner's Manager can save revisions directly. Activation creates an approval request; it does not silently change live behavior.
- New missions pin the active profile version. Existing missions retain their original version. Run details show the pinned content and recorded action results; saved progress survives a page reload.
- OpenClaw import reads only the five supported instruction documents. It excludes scripts and memory, flags legacy runtime references and redacts common credential patterns. This is not a comprehensive secret scanner. Review imported content before activation.
- Safety rules, access permissions, executable tools and actual schedules are not editable through business instructions.

## Fixes from review

- Saved drafts and imports open in the editor instead of reverting to live content.
- Revalidating active/superseded instructions no longer changes their activation state.
- Full accepted instruction text reaches the runtime instead of silently stopping at 16,000 characters.
- Common credential tokens are actually removed during import.
- Missing pinned instructions and profile lookup failures no longer silently fall back to defaults.
- Unsaved edits prompt before switching roles/tabs or unloading the page. General app navigation still needs browser-level acceptance testing.

## Verification

- Frontend production build: passed.
- `python -m pytest tests/test_agent_profiles_portable.py -q`: 3 passed. Covers redaction, rule length rejection, draft/check/activate, Manager approval, rechecking live/old versions, full text, mission pinning, saved run details and unauthorized run access.
- `git diff --check`: passed.
- Alembic: single head `e9a4c2d71f06`.
- PostgreSQL integration suite: blocked at database connection/setup locally; not passed.
- Interactive browser acceptance and deployment migration: not yet verified.

## Commit recommendation

Original recommendation: feature-branch review before main. The user subsequently approved a local main commit. Commit message: `feat: add editable agent profiles and instruction audit visibility`.

Before pushing or deploying: run PostgreSQL integration tests and the migration against a disposable database, then exercise save/open/compare/check/activate/rollback, ZIP import, Manager approval and a real run in the browser. Confirm activation affects only new missions. Do not enable imported legacy Notion/OpenClaw steps unchanged.
