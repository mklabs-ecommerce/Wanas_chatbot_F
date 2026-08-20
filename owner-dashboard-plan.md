# Wanas Gallery — Owner Dashboard: Planning

A web dashboard for the store owner (and staff, with limited access) to see analytics and chat activity across every channel the chatbot operates on — Web, WhatsApp, Instagram, TikTok, Facebook — plus a combined "All" view. Extends the existing Modular Monolith backend; does not replace or duplicate any of its logic.

---

## 1. Scope

- **Sections:** Web, WhatsApp, Instagram, TikTok, Facebook, All (aggregated) — each its own tab/page with the same layout pattern, filtered to that channel's data
- **Per-section content:** KPI cards + visualizations (see Section 3) + a read-only chat/conversation viewer (see Section 4)
- **TikTok:** built now as a placeholder/empty state (channel isn't live yet) — same UI shell as the others, but shows "Not connected yet" instead of real data
- **Auth:** Owner (full access) + Staff (limited — can view chats/orders/analytics, cannot change settings or manage other accounts) — see Section 5
- **Mobile-responsive** — the owner needs this to work properly on a phone, not just desktop
- **Language: Arabic, right-to-left (RTL) layout** — this isn't just translated labels, the whole UI direction flips (nav on the right, text alignment, chart legends, etc.)
- **Visual style:** reference screenshot provided (Oripio-style fintech dashboard) — clean white cards on light gray background, rounded corners, a single accent color, sidebar navigation, top bar with search/notifications/profile, card-based KPIs with small trend indicators, bar chart for revenue over time, transaction/activity table with status pills. Color palette confirmed in Section 6 — distinct from the storefront's black & gold, since this is an internal tool.

---

## 2. Architecture (fits the existing Modular Monolith)

This is a **new, separate frontend application** talking to the existing backend through a **new admin-facing API module** — it does not touch the customer-facing chatbot logic at all.

```
Backend (existing app/ project):
├── modules/
│   └── admin/                    # NEW — everything the dashboard needs
│       ├── auth/
│       │   ├── service.py         # login, session/token issuing, role checks
│       │   ├── repository.py      # owner/staff accounts table
│       │   └── schemas.py
│       ├── analytics/
│       │   ├── service.py         # KPI + chart data aggregation (Section 3)
│       │   └── schemas.py
│       ├── conversations/
│       │   ├── service.py         # read-only conversation listing/detail (Section 4)
│       │   └── schemas.py
│       └── router.py              # all /admin/api/* endpoints, auth-protected
│
Frontend (new, separate project):
└── dashboard/                    # React app (or confirm framework — Section 6)
    ├── pages/
    │   ├── login.tsx
    │   ├── dashboard/[channel].tsx   # web | whatsapp | instagram | tiktok | facebook | all
    ├── components/
    │   ├── KpiCard, RevenueChart, ConversationList, ConversationDetail, ...
    └── lib/
        └── api.ts                 # calls the backend's /admin/api/* endpoints
```

### Module boundary rules (same discipline as the rest of the project)

- `modules/admin/analytics` **reads** from other modules' data (orders, feedback, support tickets, chat/engagement conversations) but does so by calling their existing service functions where possible, not by querying their tables directly — if a service function doesn't exist yet for something analytics needs (e.g. "orders in date range grouped by channel"), add it to the owning module (`orders.service`), don't reach around it.
- `modules/admin/conversations` is **read-only** — it must not be able to send messages or take any action in a live conversation. That capability (if added later, per your "maybe later" answer) would be a distinct, clearly-separated write path, not bolted onto this read view.
- `modules/admin/auth` owns staff/owner accounts and sessions — no other module should implement its own auth.

---

## 3. KPIs & Analytics (per channel, and aggregated in "All")

All of these, confirmed:

- **Order count & revenue** (with trend vs. previous period, like the reference screenshot's "+3.2% from last month")
- **Average order value**
- **Top-selling products** (ranked list/table)
- **New vs. returning customers**
- **Message/conversation volume** (per channel, and combined in "All")
- **Support ticket volume & resolution** (open/in-progress/resolved counts, maybe average resolution time)
- **Customer feedback/ratings average**
- **COD vs. online payment split**

**Additional v1 KPIs** (confirmed — see Section 6 for reasoning):

- **Fabrication/escalation rate**: how often the bot creates a support ticket because it couldn't resolve something — a rising trend here is an early warning sign worth surfacing prominently, not burying
- **Conversion funnel**: comments → DMs opened → orders created — shows how many conversations actually turn into sales per channel
- **Busiest hours/days**: when conversations actually happen, useful for COD dispatch and staffing decisions

**Deferred to a later pass** (Section 6): response/handling time, image-recognition accuracy proxy.

### Visualizations (matching the reference style)

- KPI cards with a number + trend arrow/percentage (top row, like the reference's Account Balance / Total Expenses / Total Savings cards)
- Revenue-over-time bar chart (like the reference's Overview chart), filterable by time range (day/week/month/year)
- Recent activity table (like the reference's Recent Transaction table) — here, this would be recent orders or recent conversations, with a status pill (success/pending/failed equivalent — e.g. fulfilled/pending COD/cancelled)
- Top products as a ranked list or horizontal bar chart

---

## 4. Conversation Viewer (read-only, for now)

- A list of recent conversations per channel (customer identifier, last message preview, timestamp, status — e.g. "resolved," "has open ticket," "ongoing")
- Clicking a conversation shows the full transcript, same message-bubble style across all channels for consistency, even though the underlying data comes from different integrations (web sessions, WhatsApp, Instagram DMs, etc.)
- **Explicitly read-only in this version** — no reply box, no takeover button. Confirmed as a deliberate v1 scope decision, with reply/takeover planned as a clearly separate future addition, not something to half-build now.

---

## 5. Auth & Roles

- **Owner:** full access — all analytics, all conversations, account management (create/remove staff accounts), settings
- **Staff:** can view analytics and conversations, **cannot** access settings or manage other accounts
- Staff account management (creating/removing staff logins, assigning role) is an Owner-only capability within `modules/admin/auth`
- Simple, standard session/token-based login — no need for anything exotic (no SSO, no multi-tenant complexity) given this is a single-brand internal tool

---

## 6. Decisions (confirmed)

- **Frontend framework:** React — natural fit given the reference dashboard's component-heavy, chart-driven style.
- **v1 additional KPIs** (beyond the core list in Section 3): **fabrication/escalation rate**, **conversion funnel** (comments → DMs → orders), and **busiest hours/days**. Chosen because they're cheap to compute from data the backend already has, and directly useful right now (escalation rate protects the "never fabricate" principle; conversion funnel measures whether the newly-built Instagram engagement feature is working; busiest hours help with COD dispatch/staffing timing).
- **Deferred to a later pass:** response/handling time (more useful once there's real scale to monitor) and image-recognition accuracy (the underlying feature isn't fully built/tested yet — better to add this once there's real usage data to show).
- **Color scheme:** a distinct internal-tool palette, not the storefront's black & gold — keep it close to the clean, light, single-accent style of the reference screenshot. Suggest a calm blue or teal as the accent (avoiding green, to not visually imply "money/success" on every metric regardless of whether that's true) — confirm or adjust when you see the first build.
- **Date range filtering:** wanted on both the KPI cards and the chart, with a default view (e.g. "this month") plus a picker, matching the reference's "This Year" dropdown pattern.
- **Hosting:** Railway, alongside the existing backend — simplest option, one platform to manage, no cross-origin hosting complexity to set up.

---

## 7. Suggested Build Order

1. **Backend first:** `modules/admin/auth` — owner/staff accounts, login, session handling. Get this working and testable (e.g. via API calls) before any frontend exists.
2. `modules/admin/analytics` — start with the core confirmed KPIs (Section 3's first list) for ONE channel (suggest starting with "Web," since it has the most existing data from earlier testing), returning real numbers via the API before building any UI.
3. Extend analytics to the v1 additional KPIs (fabrication rate, conversion funnel, busiest hours).
4. Extend analytics to cover all channels + the "All" aggregated view, including TikTok's placeholder/empty state.
5. `modules/admin/conversations` — read-only listing + detail endpoints, one channel first, then all.
6. **Frontend scaffolding:** React app, login page wired to step 1's auth, RTL Arabic layout shell, mobile-responsive base layout matching the reference style.
7. Build the dashboard page for one channel end-to-end (KPI cards, chart, activity table, conversation list/detail) before replicating across all six sections — validate the pattern once, then repeat it.
8. Wire up date range filtering.
9. Deploy to Railway alongside the backend, confirm it works on an actual phone, not just browser dev-tools' mobile emulation.

Confirm each step's data is actually correct against real backend data before moving to the next — especially the KPI calculations, since wrong numbers on an owner's own dashboard erode trust fast.

---
