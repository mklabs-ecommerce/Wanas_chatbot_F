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

**`dist/` is gitignored and the mount is decided once, at import time.** Two
consequences:

- **Deploying anywhere (Railway included) needs `npm ci && npm run build` in `dashboard/`
  as part of the build step, before the Python service starts** — a fresh clone or a
  Python-only build has no `dist/`, `/admin` 404s, and the only sign is one log line at
  startup (`dashboard/dist not found - ...`).
- **Locally, editing a component and restarting the backend without rebuilding serves
  stale JS.** `npm run build` after every change you want reflected through FastAPI, or
  just use `npm run dev` (which needs no rebuild step) while iterating.

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
- **The conversation title is a resolved name, not raw data** — `customer_name` on
  every conversation is always a non-blank string computed backend-side
  (`admin/conversations/service.py`'s `_customer_name`): the Instagram handle when there
  is one, else a name from a support ticket or feedback row, else `"عميل " + id[:8]`.
  The frontend never falls back on its own, so `ConversationList.tsx` and
  `ConversationDetail.tsx`'s header can't disagree.
- **Instagram reply/takeover is the one write path** (owner's call, 2026-08-20) — every
  other channel, including web, stays read-only. Sending a reply from the dashboard
  pauses the bot for that conversation (`chat.agent.handle_message` checks
  `repository.is_takeover_active` right after storing the customer's turn); "رجّعها
  للبوت" clears it. A bot message and an owner message are both stored as
  `role=ROLE_MODEL` — a new role would corrupt Gemini's turn alternation once the bot
  resumes — so `ConversationDetail.tsx` tells them apart by the `author` field the
  backend derives from `provider`, not from `role`. `ConversationDetail.tsx` polls its
  open conversation every 5s while `channel === 'instagram'`, so an incoming reply shows
  without reopening the panel — safe to do here because this is internal-dashboard code,
  not the customer-facing widget the "web is out of scope for now" call was about.

## Before deploying

`ADMIN_OWNER_PASSWORD` currently guards this on `127.0.0.1` only. Putting it on a public
Railway URL (step 9) makes the same password the only thing standing between the open
internet and every customer's name, phone, address and conversation transcript — a
materially bigger exposure than what was accepted for local use. Worth changing it to
something real before that step, not after.

## What has *not* been verified

No browser or screenshot tool was available while building this, so the following are
unverified beyond "the code should do this" — check them for real before trusting them:

- Visual rendering of every screen, and that the RTL flip is actually correct on all of
  them (nav position, text alignment, the chart's legend/axis). One thing *is* confirmed
  by inspecting the built CSS rather than eyeballing it: the design tokens
  (`--color-card`, `--color-accent-600`, ...) are both defined and actually consumed by
  a rule (`grep -o 'var(--color-card)' dist/assets/*.css` returns a hit) — so the cards
  will not silently render as transparent. That is not the same as confirming they look
  right.
- Mobile layout on an actual phone, not devtools' emulator — tab strip scrolling,
  card stacking, the conversation detail panel's full-screen behaviour.
- That login actually works end-to-end in a live browser session (verified only via
  curl against both the dev proxy and the production build's static serving).
- The Instagram reply box and the 5-second poll: verified through the backend's dry-run
  path and `pytest`/`tsc`/`npm run build`, never in a live browser against a real
  Instagram thread. Send a reply from a second account and watch it arrive in-app before
  trusting the takeover/hand-back flow end to end.

Two bugs were found and fixed by reasoning rather than by seeing them, which is worth
naming precisely *because* neither would have shown up in a build or a curl check:
switching tabs used to leave a conversation's detail panel open and pointed at the old
channel's conversation id (fixed with `key={channel}` forcing a remount), and the date
presets used `Date.toISOString()`, which converts to UTC first — between midnight and
2am Cairo time, "الشهر ده" would have silently requested last month's range instead.
That second class of bug (correct-looking code, wrong at specific hours) is exactly what
a five-minute glance in a browser will not catch either — it needs someone testing near
a day boundary, or trusting the fix.
