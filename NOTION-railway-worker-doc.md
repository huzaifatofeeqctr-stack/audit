# Closed-Won Contract Audit — Railway Worker (Architecture, Changes & Runbook)

> **What this is:** the living engineering doc for the **Railway Python worker** that runs
> Postscript's Closed-Won Contract Audit — what it does, every change shipped, how it's
> configured, and how to operate it. Companion to the PRD and `SKILL.md`.
> Owner: **Huzaifa Tofeeq** · Requested by: **Caitlin Ferson** (Sales Strategy & Ops).

| | |
|---|---|
| **Status** | Built & operating (pilot). Worker live on Railway — **v0.6.2**. |
| **Worker URL** | https://audit-production-54bd.up.railway.app |
| **Repo / branch** | `huzaifatofeeqctr-stack/audit` · `claude/kind-tesla-9l7gw` |
| **GitHub** | https://github.com/huzaifatofeeqctr-stack/audit/tree/claude/kind-tesla-9l7gw |
| **Channels** | #closed-won-presales (`C08JS7N86D6`) · #sfdc-oppty-audit (`C0B88KMFJ3E`) |
| **Last updated** | June 24, 2026 |

---

## TL;DR — where it stands today

The audit is no longer a design — it's a self-contained worker running in production. Since
the v3 architecture doc, the big shifts are:

- **The worker now monitors Slack directly** via its own bot token (self-polling every 5 min).
  It no longer depends on n8n or the Slack Events API to *ingest* deals — n8n is now an
  optional/redundant second path.
- **The "contract sent for signature" notifier moved onto the worker itself**
  (`/scan-signatures`) — notify **+** auto-audit, with durable dedup via Slack history.
- **±$5 monetary tolerance** added (one-cent diffs no longer flag).
- **Slack-native rendering** — the worker converts Markdown to Slack `mrkdwn`
  (tables → aligned code blocks) so replies render cleanly.
- **Reaction failures are now non-fatal** — a missing `reactions:write` scope can never break
  an audit (shipped in v0.6.2).
- **Verdict, counts, and tagging are deterministic in code** — never trusted to the model.

> 🔒 **Security standing items:** the audit is **read-only** on Salesforce & SpotDraft (it only
> writes to Slack). The Slack bot token was pasted in plaintext during setup — **treat it as
> exposed and rotate it.** Real credentials stay out of git (`.env` is gitignored; only
> `.env.example` is committed).

---

# 1. Architecture Overview

## 1.1 Two runtimes, one brain

| Runtime | How it runs | State |
|---|---|---|
| **Manual (Cowork / Claude Code)** | A human says "audit the last message in #closed / #sfdc"; Claude uses Slack + Salesforce MCP + SpotDraft API + local PDF scripts. | Running daily; the working reference. |
| **Automated (Railway worker)** | Worker polls both Slack channels every 5 min, audits new Rattle deals, and posts back in-thread. | **Live & operating (v0.6.2).** |

Both consume the same `SKILL.md` (loaded from the branch's raw GitHub URL, 5-min cache), so the
two paths can't drift.

## 1.2 Ingestion: the worker self-polls (primary) — n8n optional (secondary)

The original design relied on the Slack Events API → n8n → HTTP → worker. We hit a hard
constraint: **a Slack app allows only one Events `request_url`**, so we couldn't point both a
#closed and a #sfdc trigger at n8n cleanly. Resolution: **the worker monitors Slack directly**
using its bot token.

```
        ┌──────────────────────── Railway worker (one process) ────────────────────────┐
        │                                                                               │
        │  background poller (every SCAN_INTERVAL = 300s)                               │
        │    • scan_once()        → audit new Rattle deals in both channels             │
        │    • scan_signatures()  → "contract sent for signature" notices + audits      │
        │                                                                               │
        │  HTTP endpoints (manual / cron triggers)                                      │
        │    GET /health · GET /whoami · POST /audit · GET|POST /scan · /scan-signatures│
        │                                                                               │
        │  per audit:  SF SOQL → contract selection → SpotDraft key_pointers + PDF      │
        │              → 16-check audit (Claude, SKILL.md system prompt)                │
        │              → deterministic verdict/counts/tags (code) → post + ✅           │
        └───────────────────────────────────────────────────────────────────────────────┘
                              │ loads SKILL.md from raw GitHub (SKILL_SOURCE)
                       GitHub repo → SKILL.md  (single source of truth)
```

**Dedup (durable, stateless-safe):** before auditing a thread the worker checks whether an audit
already exists in it (our bot's reply, or a prior audit's text signature). The signature notifier
dedups on the **T-id already announced** in Slack history. Slack *is* the worker's state — this
survives restarts.

### The two ingestion paths (both still in the repo)

| Path | Who queries Salesforce | When you'd use it |
|---|---|---|
| **Worker self-poll** (primary, live) | Worker — its own SF OAuth + Slack bot token | Default. No n8n / no Events dependency. |
| **Path A** (`n8n-pathA.workflow.json`) | Worker | If you want n8n to drive ingestion; worker still self-queries SF. |
| **Path B** (`n8n-closed-won-audit.workflow.json`) | n8n fetches SF and injects `sf` | Fallback that sidesteps the worker's SF permissions entirely. |

## 1.3 What's connected

| Connector | Mode | Used for |
|---|---|---|
| **Slack** | read + send + reactions (no delete/edit of others) | Read alert messages; post audit in-thread; ✅ on clean; @-tag owners. Corrections are superseding re-posts. |
| **Salesforce** | read-only SOQL (OAuth client-credentials for the worker) | Opportunity header + OpportunityLineItem; `SpotDraft_Contract__c` records. |
| **SpotDraft Public API** | HTTP header auth (client-id / client-secret) | `key_pointers/` + `download/` (signed PDF). Base `https://api.spotdraft.com/api/v2`. |
| **PDF text extractor** | zlib FlateDecode extraction (`clients.extract_pdf_text`) | Term dates, fees, clause wording from the SO PDF (poppler-free). |
| **git / GitHub** | this repo | `SKILL.md` = single source of truth; ships via commit/push. |

## 1.4 The two moments it covers

- **Pre-close** (Stage 5 → #sfdc-oppty-audit): audited and marked **⚠ PRELIMINARY** (contract in
  signature stage; terms may change).
- **Post-close** (Closed Won → #closed-won-presales): the executed-contract audit.

---

# 2. Railway Worker — Changelog (v0.5.2 → v0.6.2)

| Version | Change | Why |
|---|---|---|
| **0.5.2** | Baseline: full audit pipeline (SF SOQL → contract selection → SpotDraft key_pointers + PDF → 16-check via Claude → post + ✅). `dry_run` returns text without posting. | The validated golden-set baseline. |
| **0.5.x** | **Deterministic post-processing.** Verdict + counts derived from the audit table in code; tagging fully code-driven; "evidence rule" ignores placeholder key_pointers (e.g. phantom `AIPlatformFeePrice` $699). | The model's free-form counts / CLEAN line / tags were unreliable. |
| **0.5.x** | **`_to_slack()` renderer.** Converts GitHub-Markdown → Slack `mrkdwn`: headings/bold → `*bold*`, `[t](u)` → `<u|t>`, bullets → •, and the pipe table → an aligned **monospace code block** (Slack has no Markdown tables). | The worker had been posting raw Markdown that rendered badly in Slack. |
| **0.6.0** | **Self-polling.** Worker monitors both channels directly via its bot token: `scan_once()`, `_is_rattle_deal`, `_flatten_blocks` (Rattle deal fields live in message **blocks**, not `text`), `_thread_already_audited`, background `_poller`, `/scan` endpoint. Env: `SCAN_ENABLED`, `SCAN_INTERVAL`, `AUDIT_CHANNELS`, `RATTLE_USER_ID`, `BOT_USER_ID`. | Slack allows only one Events `request_url`; self-polling sidesteps the n8n single-URL problem and removes the Events dependency. |
| **0.6.0** | **±$5 monetary tolerance.** SKILL.md checks 7, 8, 10, 14, 16 changed "Amounts equal" → "Amounts within $5". Money only — dates, packages, shop IDs, inclusion stay exact. | "One cent is too close" — tiny rounding diffs shouldn't flag. |
| **0.6.1** | **Signature notifier on the worker.** `scan_signatures()` polls `SpotDraft_Contract__c` for `Signing` / `Awaiting Signature` (LAST_N_DAYS:1), posts a "Contract sent for signature" notice to #sfdc, and (when the opp resolves) threads a **PRELIMINARY** audit against the signing SO. `gather(..., force_tid=)` pins the audit to the just-signed T-id; `_resolve_opp_via_account()` links the opp when the contract's `Opportunity__c` is blank (only if exactly one in-flight opp). Dedup via Slack history (T-id already announced). `/scan-signatures` endpoint. Env: `SIG_NOTIFY_ENABLED`, `SIG_CHANNEL`, `SIG_AUTO_AUDIT`. | Rattle can't fire on this — "sent for signature" is a **child** `SpotDraft_Contract__c.Status__c` change, not an Opportunity stage move. Caitlin asked for the notice. |
| **0.6.2** | **`slack_react` is now best-effort (never raises).** A reaction failure — e.g. the bot token lacking `reactions:write` (`missing_scope`), or `already_reacted` — is logged and swallowed. The audit has already posted by the time we react, so a cosmetic ✅ can never break it. | Surfaced live: `reactions.add failed: missing_scope` on a signature audit (Psychic Samira) could otherwise bubble up as an audit error. |

### Verification done at v0.6.1/0.6.2

- `/scan` test: skipped already-audited threads, caught 3 new (PWR Mobile, Jason Wu Beauty, Wyze).
- `/scan-signatures` first run: 13 "sent for signature" notices to #sfdc; 4 auto-audited
  (RANGER STATION, PWR Mobile, ToughTested, Psychic Samira); 9 notify-only (no opp link).
  Second run returned **0 new** — Slack-history dedup confirmed.
- `/health` confirmed **v0.6.2** live after the reaction fix.

---

# 3. Endpoints & Operations Runbook

## 3.1 API

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{ ok, service, version }` — also confirms GitHub → Railway auto-deploy is live. |
| `GET /whoami` | Salesforce run-as identity + masked `SF_CLIENT_ID` fingerprint (diagnostic). |
| `POST /audit` | Body `{ channel_id, thread_ts, text [, sf] [, dry_run] }`. Runs the audit; posts in-thread + ✅ if clean. `sf` injects pre-fetched Salesforce data (Path B); `dry_run:true` returns the rendered message and posts nothing. |
| `GET\|POST /scan` | Manually poll both channels and audit any un-audited Rattle deal. |
| `GET\|POST /scan-signatures` | Manually run the signature-stage scan (notices + threaded preliminary audits). |

## 3.2 How it runs

- **Background loops** are off unless enabled. With `SCAN_ENABLED=true` and/or
  `SIG_NOTIFY_ENABLED=true`, the worker runs `scan_once()` / `scan_signatures()` every
  `SCAN_INTERVAL` (default 300s = 5 min).
- **Without those flags**, the worker only acts when you hit `/scan` or `/scan-signatures`
  (or point a Railway cron at them).
- **Timing:** new deals are audited within ~one poll interval (≤ ~5 min) once the loops are on.

## 3.3 Breaking-point analysis — what happens when a dependency fails

| Dependency | Failure mode | Worker behavior | Blast radius |
|---|---|---|---|
| **Railway (host)** | Crash / restart / redeploy | Poller restarts on startup; dedup via Slack history means **no double-posts**; already-audited threads are skipped. | Brief gap until the container is back; no data loss. |
| **Slack API / bot token** | Token revoked or `chat:write` missing | `slack_post` raises → audit can't be delivered; `/scan` records the error per channel. | **Hard outage** — nothing posts. Rotate/repair token. |
| **Slack `reactions:write` scope** | Scope missing (`missing_scope`) | **Non-fatal (v0.6.2)** — logged, swallowed. Audit still posts; only the ✅ is absent. | Cosmetic only. |
| **Claude API (Anthropic)** | Down / key invalid / rate-limited | `run_claude` raises → real runs post a short `⚠ Audit worker error` note *in-thread* (never to channel root); `dry_run` posts nothing. Since no audit reply lands, the thread stays "un-audited" and is **retried next poll**. | Deal audit delayed, not lost. |
| **Salesforce** | Down / token expired / FLS or perm gap | `soql` raises (surfacing the SF error body) → audit fails with an in-thread note; signature scan returns an error entry. | Audit delayed; retried next poll. |
| **SpotDraft API** | Down / contract not retrievable | `key_pointers_error` / `pdf_error` captured in the bundle; the audit still runs on partial data (degraded — may produce warnings rather than a clean ✅). | Degraded audit, not a crash. |
| **n8n** | Down | **No impact** on the primary path — the worker self-polls. n8n is only the optional secondary ingestion. | None (primary path independent). |
| **GitHub (SKILL.md source)** | Raw URL unreachable | `load_skill()` falls back to the bundled local `SKILL.md`; last good copy is cached 5 min. | None (graceful fallback). |

## 3.4 How to monitor

- `GET /health` → confirm `version`.
- After a `SKILL.md` edit: no redeploy needed (raw-URL fetch, 5-min cache). After a code change:
  push to the branch → Railway auto-deploys → poll `/health` until `version` bumps.
- Spot-check a posted audit: PRELIMINARY banner present only for signing-stage; table renders as
  a monospace block; clean deals get ✅ and no @-mention.

---

# 4. Configuration & Environment Variables

Set in **Railway → Variables**. Secrets stay out of git.

| Variable | Default | Purpose |
|---|---|---|
| `SF_INSTANCE_URL` | `https://postscript.my.salesforce.com` | Salesforce instance. |
| `SF_CLIENT_ID` / `SF_CLIENT_SECRET` | — | Salesforce client-credentials OAuth (connected app `3MVG9…gB1M49`, run-as "Spotdraft Bot"). |
| `SPOTDRAFT_CLIENT_ID` / `SPOTDRAFT_CLIENT_SECRET` | — | SpotDraft Public API headers. |
| `ANTHROPIC_API_KEY` | — | Claude (audit generation). |
| `SLACK_BOT_TOKEN` | — | `xoxb-` token; needs `chat:write`, `channels:history`, and `reactions:write` (see below). |
| `SLACK_BOT_USER_ID` | — | `U0B8RPYQ2GL` — enables thread dedup. |
| `AUDIT_CHANNELS` | `C08JS7N86D6,C0B88KMFJ3E` | Channels the poller scans. |
| `RATTLE_USER_ID` | `U05AA8MBV9B` | Only audit deals posted by Rattle. |
| `SCAN_ENABLED` | off | **Set `true`** to enable the audit poller in the background loop. |
| `SIG_NOTIFY_ENABLED` | off | **Set `true`** to enable the signature notifier in the loop. |
| `SCAN_INTERVAL` | `300` | Poll cadence (seconds). |
| `SIG_CHANNEL` | `C0B88KMFJ3E` | Channel for "sent for signature" notices (#sfdc). |
| `SIG_AUTO_AUDIT` | `true` | Auto-audit when the opp resolves; otherwise notify-only. |
| `SKILL_SOURCE` | raw GitHub URL | Where `SKILL.md` is loaded from. |
| `AUDIT_MODEL` | `claude-opus-4-8` | Model for the audit. |

## 4.1 To run hands-off

1. **Slack:** add the **`reactions:write`** bot scope (currently only `reactions:read`) and
   **reinstall** the app — otherwise the ✅ silently won't post (audits still work).
2. **Railway:** set `SCAN_ENABLED=true` and `SIG_NOTIFY_ENABLED=true`.
3. Optional: instead of the in-process loop, point a Railway cron at `/scan` and `/scan-signatures`.

> 🔒 Rotate the Slack bot token (pasted in plaintext during setup).

---

# 5. SKILL.md — The Audit Brain

`SKILL.md` is the system prompt and single source of truth, shared by the manual and automated
paths. The worker loads it from the branch's raw GitHub URL (`SKILL_SOURCE`, 5-min cache), so a
merged edit takes effect **with no redeploy**.

**Key rules currently encoded:**

- **The full 16-check audit must always run, and *every* mismatch must be reported** — not just
  the first/minimum. (Added after the **Allegory** miss, where only the minimum was reported and a
  $100 platform-fee mismatch slipped through.)
- **±$5 monetary tolerance** on checks 7, 8, 10, 14, 16. Money only; dates, packages, shop IDs,
  and product inclusion stay exact.
- **Auto-renewal (§4a):** a Renewal that reaches Closed Won with no new SO is audited against the
  auto-renewing prior-term SO (the one explicit exception to "no prior-term fallback").
- **Multi-shop minimum allocation:** master SOs matched on the opp's own `Shop_ID__c` with
  per-shop minimum-commitment allocation (validated on the Hanks Belts / Woolx $45k-qtr master).
- **Contract selection heuristic:** newest Completed Proposed Service Order for the account;
  Signing → ⚠ PRELIMINARY; no prior-term fallback; draft-only → "not auditable."
- **Evidence rule:** product inclusion is decided only by an actual addendum in the PDF **and** the
  matching SFDC line item — placeholder key_pointers are ignored.
- **Tagging (deterministic in code, not the model):** clean ⇒ no @-mention (just ✅);
  Renewal/Upsell → Caitlin (`U077EJVK10R`); New Business/Winback/Captured Account/Amendment →
  Lola (`U07GQE3BP7F`); + Viv (`U08CPAGU1DZ`) only if Postscript Plus, **never in #sfdc**.
- **Result tokens:** ✅ Match · ❌ Mismatch · ❌ Missing in SFDC · ⚠ note · ➖ N/A · ⛔ Blocked.

---

# 6. Action Items & Open Roadmap

## 6.1 Huzaifa — spec-doc tasks (carried in)

- [ ] **Add a high-level summary to the spec doc** covering: **trigger channels**, **message
  format**, **audit timing**, and a **breaking-point analysis** for every dependent tool
  (n8n, Slack, Claude API). — *Due: end of day.* (Draft content ready in §6.2 below — paste in.)
- [ ] **Add live links** to the N8N workflow and GitHub in the spec doc. — *Due: tomorrow morning
  before the 1:1.* (GitHub: https://github.com/huzaifatofeeqctr-stack/audit/tree/claude/kind-tesla-9l7gw — N8N workflow URL TBD.)

## 6.2 Draft: high-level summary for the spec doc

**Trigger channels.** Two Slack channels, both watched by the worker's poller:
#closed-won-presales (`C08JS7N86D6`, post-close executed audits) and #sfdc-oppty-audit
(`C0B88KMFJ3E`, pre-close ⚠ PRELIMINARY audits + "sent for signature" notices).

**Message format.** A threaded reply under the Rattle alert: a one-line **Result** header
(`X of 16 checks passed — N mismatches, M warnings`) followed by the 16-check table rendered as an
aligned monospace block, links to the Opportunity/Account, and — only on findings — an @-mention
of the routed owner. Clean deals get a ✅ reaction and no mention.

**Audit timing.** The worker polls every 5 min (`SCAN_INTERVAL=300`), so a new deal is audited
within roughly one interval (≤ ~5 min) of posting. Signature notices follow the same cadence.

**Breaking-point analysis.** See the table in §3.3 — covers Railway, Slack (token + reaction
scope), Claude API, Salesforce, SpotDraft, n8n, and the SKILL.md source. Headlines: the primary
path is independent of n8n; reaction-scope failures are non-fatal; Claude/SF/SpotDraft failures
delay (and auto-retry) an audit rather than losing it; a missing/revoked Slack token is the only
hard outage.

## 6.3 Roadmap / open items

| Priority | Item | Status |
|---|---|---|
| P0 | Add `reactions:write` Slack scope + reinstall; set `SCAN_ENABLED` / `SIG_NOTIFY_ENABLED` in Railway | ⏳ ready to flip on |
| P0 | Rotate the exposed Slack bot token | ⏳ open |
| P1 | Deterministic opp→contract link (SpotDraft ID on opp, or `Opportunity__c` populated) to retire the heuristic | ◻ open (~half-populated) |
| P1 | Fondue / Gimme separate-order-form lookup so they audit cleanly | ◻ open |
| P2 | Tighten SKILL.md table-cell verbosity to avoid Slack auto-splitting long audits | ◻ open |
| P2 | Lola on every New Business vs. only actionable findings | ◻ pending Caitlin/Lola |
| P2 | Formal golden-set regression gate on SKILL.md PRs | ◻ open |
