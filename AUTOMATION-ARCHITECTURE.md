# Closed-Won Contract Audit — Automation Architecture

| | |
|---|---|
| **From** | interactive Claude Code → **n8n orchestration + Railway Python worker (built & operating)** |
| **Status** | **v3 — Built & operating (pilot).** Worker live on Railway (`v0.5.2`); n8n workflows built & importable; Path A (worker self-queries Salesforce) validated end-to-end. |
| **Last updated** | June 22, 2026 |
| **Companion docs** | PRD v2.0 (Closed-Won Contract Audit Agent), SKILL.md (audit spec / system prompt) |
| **Repo** | `huzaifatofeeqctr-stack/audit` (branch `claude/kind-tesla-9l7gw`) |
| **Owner** | Huzaifa Tofeeq · Requested by: Caitlin Ferson (Sales Strategy & Ops) |

---

## 0. TL;DR (what changed since Design v2)

The split design is **no longer just a design — it's built and running**:

- **Railway Python worker is live** at `https://audit-production-54bd.up.railway.app` (`v0.5.2`). It does the whole audit: Salesforce SOQL → contract selection → SpotDraft `key_pointers` + PDF parse → deterministic 16-check engine → Claude (SKILL.md system prompt) → posts the threaded reply and adds the ✅ reaction itself.
- **n8n is the orchestration layer**, with **three importable workflows** in the repo (see §3.2).
- **Two viable wiring paths**, both built and tested:
  - **Path A — worker self-queries Salesforce** (`n8n-pathA.workflow.json`). n8n just listens + parses + calls the worker. *This is now fully working* after the Salesforce permission work in §5.4.
  - **Path B — n8n fetches Salesforce and injects it** (`n8n-closed-won-audit.workflow.json`). Sidesteps the worker's SF permissions entirely; useful as a fallback.
- **New: a "contract sent for signature" notifier** (`n8n-signature-notification.workflow.json`) — Rattle couldn't fire on this because it's a child-object status change, not an Opportunity stage change. This polls SpotDraft contract status directly. (See §10.)
- The audit logic still lives in **SKILL.md in git**, shared by the manual (Cowork) and automated (worker) paths.
- The worker now makes **verdict, counts, and tagging deterministic in code** (not trusted to the model) — see §5.4 / §6.2.

The manual Cowork loop still runs daily and has audited **dozens of live deals** across both channels (Branch, CardoMax, Transparent Labs, The Diesel Dudes, Azuna, Ruggable Australia, the Hanks Belts/Woolx multi-shop master, Eleven Eleven, MyLove MyTreasure, etc.). That's the baseline the worker reproduces.

---

## 1. Where we are today

### 1.1 Two parallel runtimes, one brain

| Runtime | How it runs | State |
|---|---|---|
| **Manual (Cowork / Claude Code)** | Human says "audit the last message in #closed / #sfdc"; Claude uses Slack + Salesforce MCP + SpotDraft API + local PDF scripts. | Running daily; the working reference. |
| **Automated (n8n + Railway worker)** | Rattle posts → n8n trigger → HTTP → worker → worker posts back. | **Worker live & validated; n8n workflows built, ready to activate.** |

Both consume the same **SKILL.md** so outputs can't drift.

### 1.2 What's connected

| Connector | Mode | Used for |
|---|---|---|
| Slack | read + send + reactions (no delete/edit) | Read alert messages; post audit in-thread; ✅ reaction on clean; @-tag owners. Corrections are superseding re-posts. |
| Salesforce | read-only SOQL (read-only MCP for Cowork; OAuth client-credentials for the worker) | Opportunity header + OpportunityLineItem; SpotDraft_Contract__c records. |
| SpotDraft Public API | HTTP header auth (`client-id`/`client-secret`) | `GET /public/contracts/{T-id}/key_pointers/` + `POST /public/contracts/{T-id}/download/` (signed PDF). Base `https://api.spotdraft.com/api/v2`. |
| PDF text extractor | zlib FlateDecode extraction (worker `clients.extract_pdf_text`; Cowork `/tmp/pdffind.py`) | Read Term dates, fees, clause wording from the SO PDF (poppler-free). |
| git | this repo | SKILL.md = single source of truth; ships via commit/push. |

### 1.3 The two moments it covers
- **Pre-close** (Stage 5 → `#sfdc-oppty-audit`): audited and marked **⚠ PRELIMINARY** (contract in signature stage; terms may change).
- **Post-close** (Closed Won → `#closed-won-presales`): the executed-contract audit.

---

## 2. Requirements for "fully automated" (status)

| Requirement | Status |
|---|---|
| Fire automatically within ~5–10 min of an alert in both channels | ⏳ Workflows built; flip on by activating in n8n |
| Run unattended, durably, idempotently (dedupe, survives restarts) | ✅ Worker durable on Railway; dedupe via thread bot-reply check + signature-notifier static-data |
| Reliable PDF parsing + deterministic, testable 16-check logic | ✅ zlib extractor in worker; checks run via SKILL.md, counts/verdict derived in code |
| Tunable rules without code deploys (SKILL.md) | ✅ Worker loads SKILL.md from raw GitHub URL (`SKILL_SOURCE`), 5-min cache |
| Same logic for manual + automated | ✅ Shared SKILL.md |
| Tagging rules + channel override encoded in the system, not heads | ✅ Deterministic in the worker (§6.2) |
| Read-only on Salesforce/SpotDraft | ✅ Worker only writes to Slack |

---

## 3. Target architecture (as built)

```
                 Slack (Rattle posts)
                        │  message event
        ┌───────────────▼────────────────┐
        │  SLACK APP  (Events API + bot)  │   "Audit Bot" creds in n8n (listen);
        └───────────────┬────────────────┘   worker posts as "Postscript Audit Agent"
                        │ webhook
        ┌───────────────▼────────────────┐         ORCHESTRATION (control plane) — n8n
        │              n8n               │   2× Slack Trigger (#closed C08JS7N86D6,
        │  - Slack Trigger ×2            │            #sfdc C0B88KMFJ3E)
        │  - IF: Rattle + *Name:* (read  │   parse *Name:* → POST worker
        │       BLOCKS, not just text)   │   (Path A) or fetch SF + inject (Path B)
        │  - Parse deal (Code)           │   retry 3× / 5s on transient 5xx
        │  - [Path B only] 3× SF queries │
        │  - HTTP → Railway worker /audit│
        └───────────────┬────────────────┘
                        │ HTTPS POST {channel_id, thread_ts, text [, sf]}
        ┌───────────────▼────────────────┐         COMPUTE (work plane) — Railway
        │   RAILWAY — FastAPI worker      │   GET /health · GET /whoami · POST /audit
        │   main.py / audit.py / clients  │   • SF SOQL (OAuth client-credentials)
        │   - SF client (or injected sf)  │   • contract selection (§6.0 heuristic)
        │   - SpotDraft client + PDF parse│   • SpotDraft key_pointers + PDF text
        │   - Claude (SKILL.md system)    │   • 16-check audit (model) →
        │   - deterministic post-process  │     deterministic verdict/counts/tags (code)
        │   - posts to Slack + ✅ itself  │   • dry_run returns text (no post)
        └───────────────┬────────────────┘
                        │ loads SKILL.md from raw GitHub (SKILL_SOURCE), 5-min cache
        ┌───────────────▼────────────────┐
        │  GitHub repo  →  SKILL.md       │   single source of truth (PR-reviewed)
        └─────────────────────────────────┘

   ── separate workflow ───────────────────────────────────────────────
   n8n Schedule (10 min) → SF query: SpotDraft_Contract__c Status IN
   ('Signing','Awaiting Signature') → dedup → post "sent for signature"
   to #sfdc-oppty-audit.   (n8n-signature-notification.workflow.json)
```

### 3.1 Component responsibilities

| Concern | Owner | Notes |
|---|---|---|
| Event ingestion | Slack app → n8n Slack Trigger | Two triggers, one per channel. |
| Parse / route / dedup / retry | n8n | Extract `*Name:*` **from message blocks** (see §4.1); IF = Rattle sender (`U05AA8MBV9B`) + `*Name:*`; HTTP retry 3×/5s. |
| Salesforce reads | Railway worker (Path A) **or** n8n (Path B) | OAuth client-credentials (worker) / OAuth2 user (n8n). Always fresh at run time. |
| Contract selection | Railway worker | §6.0 heuristic; SIGN → PRELIMINARY; no prior-term fallback; draft-only → not auditable. |
| Contract retrieval + parsing | Railway worker | `key_pointers` + `download/`; **Term dates from PDF text**, not key_pointers. |
| 16 checks | Railway worker (Claude w/ SKILL.md) | Then verdict/counts derived deterministically from the table in code. |
| Post to Slack + tag + ✅ | Railway worker | Threaded reply; deterministic tagging (§6.2); ✅ reaction on clean. |
| Audit rules (tunable) | SKILL.md in git | Worker loads via `SKILL_SOURCE` raw URL. |

### 3.2 Built artifacts in the repo

| File | What it is |
|---|---|
| `railway-worker/` | FastAPI worker: `main.py` (routes, dry_run, deterministic verdict/tags), `audit.py` (gather, SKILL.md prompt, post-process), `clients.py` (SF OAuth + SOQL, SpotDraft key_pointers/PDF, Slack), `requirements.txt`, `Procfile`, `railway.json`, `.env.example`. |
| `n8n-pathA.workflow.json` | **Path A** — triggers → IF → Parse → call worker with `{channel_id, thread_ts, text}`. Worker queries SF itself. |
| `n8n-closed-won-audit.workflow.json` | **Path B** — triggers → IF → Parse → 3× SF queries → inject `sf` → call worker. |
| `n8n-signature-notification.workflow.json` | Schedule → SF (signature-stage contracts) → dedup → notify #sfdc. |
| `n8n-closed-won-audit.GUIDE.md` | Setup / credentials / env-var guide. |
| `SKILL.md` | The 16-check spec + auto-renewal, multi-shop, tagging, output rules. |

---

## 4. Why a Slack app on n8n (not the Cowork MCP)

Unchanged from v2: you need to **listen**, not just call; the app + n8n run 24/7; scoped service identity. **Setup essentials confirmed live:** bot invited to `#closed-won-presales` (`C08JS7N86D6`) and `#sfdc-oppty-audit` (`C0B88KMFJ3E`); **Postscript Audit Agent** (`U0B8RPYQ2GL`) is now a member of both channels and posts the audits.

### 4.1 ⚠ Rattle payload gotcha (learned live — important)
The Rattle Slack event's top-level `text` is **only the headline** (e.g. "Paige Cunningham just closed Eleven Eleven for $726.60"). **All deal fields — including the `*Name:*` line — live in the message `blocks`** (and `attachments`), HTML-escaped (`&gt;*Name:*`). The MCP/Slack UI *flattens* blocks into text, which masks this; the raw webhook does not. Therefore:
- The n8n **IF** node's `*Name:*` test must scan the whole payload: left value `{{ JSON.stringify($json) }}` (not `{{ $json.text }}`).
- The **Parse (Code)** node must gather text from `blocks` + `attachments`, decode `&gt;`/`&lt;`/`&amp;`, then regex `*Name:*`, and pass the assembled text on (so the worker can re-parse the name). It returns `[]` for non-deal messages (skips quietly).

---

## 5. Why split: n8n orchestration + Railway worker

§5.1–5.3 unchanged in principle (n8n = glue; Python = brain: real PDF libs, deterministic & testable math, field-mapping landmines in one place, version control, reusable CLI, cost control). New material below.

### 5.4 Salesforce access for the worker (the build saga — resolved)
The worker authenticates via a **client-credentials connected app** (Consumer Key `3MVG9…gB1M49`) that runs as the integration user **"Spotdraft Bot"** (`ops-support+sdbot@postscript.io`, `005Uw00000W5KoUIAV`), whose profile is **"Minimum Access - Salesforce"** — so *all* access comes from assigned permission sets. Getting Path A working required, in order (each surfaced as a distinct API error and was fixed):

1. **Object read** on `Opportunity` → cleared `INVALID_TYPE: sObject 'Opportunity' is not supported`.
2. **API Enabled** system permission → cleared `API_CURRENTLY_DISABLED`.
3. **Field-level read** on every queried field → cleared `INVALID_FIELD: No such column 'Type'`.
4. **Object read** on `SpotDraft_Contract__c`.
5. **OpportunityLineItem visibility** — it has *no* standalone object permission; access derives from **Opportunity read + Price Book access**. (The worker also fetches line items via the parent subquery `(SELECT … FROM OpportunityLineItems)` as a belt-and-suspenders.)
6. **`View All` records** on `Opportunity` + `SpotDraft_Contract__c` — without it the integration user only saw *shared* records and returned "could not find" on perfectly real deals (e.g. Reale Actives). This was the final unlock; record visibility is now complete.

> Diagnostic endpoint: `GET /whoami` returns the run-as identity + a masked `SF_CLIENT_ID` fingerprint, which is how the run-as user and credential were confirmed.

**Path B exists precisely so none of this is on the critical path** — n8n's own Salesforce OAuth2 user already sees everything, so Path B worked before the permission set was finished.

---

## 6. End-to-end request flow

### 6.0 Contract selection (the riskiest join — current status)
- **No deterministic opp→contract link is used by the audit.** The worker resolves the Opportunity by **name from the Rattle `*Name:*` line**, then picks the governing `SpotDraft_Contract__c` for that account by heuristic.
- `SpotDraft_Contract__c.Opportunity__c` is now populated on **~half** of signature-stage contracts (and null on the newest), so it's still **not reliable enough** to drive selection — confirmed in §10's notifier work. The roadmap item to make it deterministic stands.
- Heuristic: newest **Completed** Proposed Service Order for the account (service-order-named); **SIGN/Signing → ⚠ PRELIMINARY**; **no prior-term fallback**; **draft-only → "not auditable."** Multi-shop master SOs are matched on the opp's own `Shop_ID__c` with per-shop **Minimum Commitment Allocation** (validated live on the Hanks Belts / Woolx $45k-qtr master → $22.5k/qtr = $7,500/mo per shop).
- **Auto-renewal (§4a in SKILL):** a Renewal that reaches Closed Won with no new SO is audited against the auto-renewing prior-term SO (the one explicit exception to "no prior-term fallback").

### 6.1 The flow (as built)
1. Rattle posts → Slack app → n8n webhook.
2. n8n IF (Rattle sender + `*Name:*` in blocks) → Parse (Code, reads blocks) → opp name + channel + thread_ts + assembled text.
3. **Path A:** POST worker `/audit { channel_id, thread_ts, text }`. **Path B:** run 3 SF queries, POST `/audit { …, sf:{opp,lineItems,contracts} }`.
4. Worker: resolve opp (SOQL; apostrophe-safe) → line items → select contract (§6.0) → `key_pointers` + PDF → extract terms (dates from PDF) → run 16 checks via Claude (SKILL.md system prompt) → **derive verdict/counts/tags in code** → post threaded reply, add ✅ if clean.
5. `dry_run:true` returns the rendered message instead of posting (used for testing — **no Slack writes**).
6. "Not yet auditable" (sync lag) → re-check later (n8n schedule, ≤3 tries) — or it simply re-fires when the deal next posts.

### 6.2 Deterministic post-processing (new — why the worker is trustworthy)
The model writes the table; **code decides the rest** (the model's free-form counts/verdict were unreliable):
- **Clean verdict & counts** are computed by counting ❌/⚠/⛔ **in the table rows only**, and the `Result:` line is rewritten to match. A message with no real table (not-found / gate-failed / not-auditable) is `clean=false`.
- **Tagging is fully code-driven:** strip any tags the model emitted, then route by opp **Type** — Renewal/Upsell (Existing Business) → **Caitlin** (`U077EJVK10R`); New Business/Winback/Captured Account/Amendment → **Lola** (`U07GQE3BP7F`); **+ Viv** (`U08CPAGU1DZ`) only if Postscript Plus **and never in #sfdc**. **Clean ⇒ no @-mention** (just the ✅ reaction).
- **Evidence rule:** placeholder `key_pointers` (e.g. a phantom `AIPlatformFeePrice $699`) are **ignored** — product inclusion is decided only by an actual addendum in the PDF **and** the matching SFDC line item. This stopped clean deals from flapping to spurious ⚠.
- **Errors never post during `dry_run`;** real-run errors post a short note **in-thread**, never to the channel root.

Result tokens: ✅ Match · ❌ Mismatch · ❌ Missing in SFDC · ⚠ \<note\> · ➖ N/A — not in contract · ⛔ Blocked.

---

## 7. SKILL.md as the shared brain + git update workflow

Unchanged in principle. The worker uses **Pattern B by default in practice**: it loads SKILL.md from the branch's **raw GitHub URL** (`SKILL_SOURCE`) with a 5-minute cache, so a merged SKILL.md edit takes effect with no redeploy. Pattern A (bundle at deploy) remains available by pinning to a commit. Recent SKILL.md changes shipped this way: the **full-16-check / report-every-mismatch** rule (after the Allegory "$100 platform fee not reported" miss), the **auto-renewal §4a** handling, the **multi-shop minimum-allocation** rule, and the **tagging/CTA** rules.

---

## 8. Cross-cutting concerns (current)

- **Secrets** (Railway env / n8n creds): `SF_INSTANCE_URL`, `SF_CLIENT_ID/SECRET`, `SPOTDRAFT_CLIENT_ID/SECRET`, `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_BOT_USER_ID` (=`U0B8RPYQ2GL`, enables dedupe), optional `SKILL_SOURCE`, `AUDIT_MODEL`. Real creds stay out of git (`.env` gitignored; only `.env.example` committed).
- **Read-only guarantee:** worker writes only to Slack. Slack has **no edit/delete** via this integration — corrections are superseding re-posts. (We hit this when test error-messages posted to #closed and could only be removed with the *posting bot's own* token, because a token can only delete messages it authored.)
- **Idempotency/dedup:** worker checks the thread for an existing bot reply (`SLACK_BOT_USER_ID`) before posting; signature-notifier dedups on contract Id in workflow static data.
- **Re-pull freshness:** always re-query SFDC at run time (a stale cached `Minimum_Spend__c` once mismatched after a server-side fix — Woolx).
- **PDF reliability:** Term dates parsed from PDF text; `key_pointers.ContractStartDate` treated as untrusted (~1 month early — Tubby Todd, Dossier).
- **Known edge cases (flag, don't hard-fail):** Fondue/Gimme sign on separate order forms (flag "confirm separate order form"); Plus `Number_Of_Months__c` rounding (±~2 weeks, low-severity ⚠); ramp/intro minimum discounts (match steady-state). **Multi-shop master synced under one account** won't be found when auditing a sibling account in Path A (e.g. Branch CA under Branch) — known limitation.
- **Model-output variance:** mitigated by the deterministic post-processing in §6.2; substance (which SO, which field, the fix) has matched the manual audits across the validation set.

---

## 9. Migration / build order — status

| Step | Status |
|---|---|
| 1. Extract logic into Railway worker (SF client, SpotDraft + PDF, selection heuristic, 16-check, Claude + SKILL.md) | ✅ Done — live `v0.5.2`, validated via `dry_run` against the golden set |
| 2. Stand up Slack app + n8n trigger/dedup/route → worker over HTTP; shadow-test | ✅ Workflows built (Path A + Path B); worker tested in dry-run |
| 3. Enable #closed-won (post-close), then #sfdc (pre-close, PRELIMINARY, no-Viv/CTA-only) | ⏳ Activate in n8n (bot already in both channels) |
| 4. Scheduled re-checks for "not yet auditable" + dedup datastore | ◻ Retry wired in HTTP node; scheduled re-check optional |
| 5. SKILL.md update flow (raw-URL fetch live; golden-set regression gate on PRs) | ✅ Raw-URL fetch live; ◻ formal CI regression gate |
| 6. **(new) Contract "sent for signature" notifier** | ✅ Built (`n8n-signature-notification.workflow.json`) — activate to go live |

### 9.1 Roadmap / open items

| Priority | Item | Status |
|---|---|---|
| P0 | Activate the n8n workflow (Path A) + signature notifier; confirm dedupe + retry in production | ⏳ ready to flip on |
| P0 | Encode §6 tagging (incl. #sfdc no-Viv + CTA-only) | ✅ deterministic in worker |
| P1 | Deterministic opp→contract link (SpotDraft ID on opp, or `Opportunity__c` populated) to retire the heuristic | ◻ open (still ~half-populated) |
| P1 | Fondue/Gimme separate-order-form lookup so they audit cleanly | ◻ open |
| P2 | Lola on every NB vs only actionable findings | ◻ pending Caitlin/Lola |
| P2 | Auto-capture per-shop allocation table for multi-shop masters | ◻ open |

---

## 10. New: "Contract sent for signature" notifier

**Problem (raised by Caitlin, 2026-06-18):** the Rattle notification for "contract sent for signature" never fired. **Why:** Rattle triggers on **Opportunity** stage/field changes, but a contract going out for signature changes a child **`SpotDraft_Contract__c.Status__c`** — the Opp doesn't move, so Rattle never sees it.

**Solution (`n8n-signature-notification.workflow.json`):**
```
Schedule (every 10 min) → SF query:
   SpotDraft_Contract__c WHERE Status__c IN ('Signing','Awaiting Signature')
   AND LastModifiedDate = LAST_N_DAYS:1
→ Code (dedup by contract Id in workflow static data; format)
→ Slack post → #sfdc-oppty-audit
```
- **Statuses that count as "sent for signature":** `Signing` (≈265 live) and `Awaiting Signature` (≈366).
- **Notify-only** (per decision): the message surfaces the event (account, T-id, contract link, opportunity if linked); audits keep firing off the existing Stage-5 path. It does **not** auto-trigger an audit because the contract→opp link is empty about half the time and account→opp resolution is unreliable.
- **Dedup:** each contract Id notifies once (bounded static-data set), so the overlapping daily query window never double-posts.
- **Optional upgrades:** switch the trigger to `SpotDraft_Contract__History` status-transitions (handles re-sends precisely, needs history-object read for the n8n SF user); or add an auto-audit branch when the opp resolves.

---

## Appendix — worker API

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{ ok, service, version }` — also used to confirm GitHub→Railway auto-deploy. |
| `GET /whoami` | Salesforce run-as identity + masked `SF_CLIENT_ID` fingerprint (diagnostic). |
| `POST /audit` | Body `{ channel_id, thread_ts, text [, sf] [, dry_run] }`. Runs the audit; posts in-thread + ✅ if clean. `sf` injects pre-fetched Salesforce data (Path B); `dry_run:true` returns the rendered message and posts nothing. |
