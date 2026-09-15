# AZKT v2 handoff

Open `AZKT v2 Direction Set -Liquid Glass II-.dc.html` in a browser (keep `support.js` and `gen/` beside it). Top strip switches role (Owner / Manager / Mechanic) and device (Desktop / Mobile).

- `AZKT v2 Build Spec.md` — entities, fields, authority rules, screen map. Start here for the backend.
- `AZKT v2 Decision Log.md` — why things are the way they are, and what is still open.
- `CLAUDE.md` — UI rules the frontend must keep.
- `AZKT Email Reminders.dc.html` — the four reminder emails.

Seed data lives in the component class (`initial()`, `seedLeads()`, `seedTasks()`, `vehicles()`); every `// ----` block in `renderVals()` is one screen's view model.
