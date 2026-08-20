# Wanas Gallery — Owner Dashboard

React + TypeScript + Vite frontend for `modules/admin` (see `../owner-dashboard-plan.md`).
Arabic, right-to-left, no language toggle — the plan called for the whole interface to
read as Arabic, not just translated labels.

## Running it

```
npm install
npm run dev      # http://localhost:5173/admin/ — proxies /admin/api/* to :8000
```

The Python backend (`../run.ps1`) must be running separately; the dev server only
proxies API calls, it doesn't serve them.

```
npm run build     # writes dashboard/dist — FastAPI serves this at /admin in production
```

`app/main.py` mounts `dashboard/dist` at `/admin` automatically once it exists. Nothing
else to configure — same origin, no CORS, matching the plan's "one platform, no
cross-origin complexity" decision (Section 6).

## Decisions worth knowing before changing this

- **No router library.** The only "pages" are the login screen and the dashboard shell;
  channel/tab selection is React state, not a URL. That's also why the FastAPI mount
  needs no SPA-fallback route — there is no deep link to fall back *from*.
- **RTL via `dir="rtl"` on `<html>` + Tailwind's logical-property utilities**
  (`ms-*`/`me-*`/`ps-*`/`pe-*`, `justify-start`/`justify-end`), not mirrored physical
  margins. A component styled with `ml-*`/`mr-*` instead will *not* flip correctly —
  that's the bug to watch for in any new component.
- **Numbers and dates stay LTR inside Arabic text** (`.ltr-num` in `index.css`), and the
  revenue chart's axis is deliberately locked `dir="ltr"` — a time axis running
  newest-to-oldest right-to-left reads as more confusing than one that briefly breaks
  page direction. This one is a judgement call, not a certainty — worth an actual look.
- **The "All" tab is analytics only** (owner's call, 2026-08-20) — `Dashboard.tsx` never
  renders a conversation list when `channel === 'all'`, even though the backend's
  `/admin/api/conversations/all` endpoint exists and works; a merged cross-channel
  conversation list wasn't wanted.
- **`ADMIN_OWNER_PASSWORD` always wins** (owner's call, 2026-08-20) — there is
  deliberately no change-password screen here. Changing the owner password is
  edit-`.env`-and-restart, on the backend.

## What has *not* been verified

No browser or screenshot tool was available while building this, so the following are
unverified beyond "the code should do this" — check them for real before trusting them:

- Visual rendering of every screen, and that the RTL flip is actually correct on all of
  them (nav position, text alignment, the chart's legend/axis).
- Mobile layout on an actual phone, not devtools' emulator — tab strip scrolling,
  card stacking, the conversation detail panel's full-screen behaviour.
- That login actually works end-to-end in a live browser session (verified only via
  curl against both the dev proxy and the production build's static serving).
