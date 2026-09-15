# AZKT project rules
- Chips, badges, pills, buttons, segmented items, and action links (`<a>`) never wrap internally: always `white-space:nowrap;flex:none`. If a row is tight, truncate the title or move the chip to its own line — never break words inside a chip.
- v2 direction (Sep 12 brief): light is the default theme; dark is a toggle (top bar, persisted). Colors live in CSS variables (--canvas, --surface, --text, --act, --amber…). Amber (#E6B35C) is a brand detail only; the action color is charcoal (--act). Four health colors only (blocked, risk, ok, wait), always paired with a text label.
- Flat surfaces, separators before containers, no nested cards, no gradients or glass. Radii 8–12px. Body 15px desktop, 16px mobile; tabular numerals for money, times, identifiers.
- Vehicles default to a list; boards are a per-view option with fixed column order and a Move stage button equivalent to dragging.
- Ask AZKT is closed by default and shares the single right inspector with Source details; never a second narrow column.
- Plain-language labels in routine screens (Home, Activity, Settings, Sales, Shop, Procedure, Automation activity); technical terms (payload, run, policy version, role) only inside expanders.
- Typeface: Instrument Sans, Noto Sans JP fallback.
- Every visible control works or is explicitly disabled with a reason; prototype shortcuts sit outside the app frame.
- Legacy files (AZKT Prototype, P0/P1/P2 Screens, 00 Components) keep the old dark direction for reference; new work goes in v2 files.
