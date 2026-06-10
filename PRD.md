# Closed-Won Contract Audit — Product & Design Doc (PRD)

**Version:** 2.0
**Last updated:** 2026-06-10
**Owner:** Huzaifa Tofeeq · **Requested by:** Caitlin Ferson (Sales Strategy & Ops)
**Status:** Live (operated manually via Claude Code today; automation in build)

---

## 1. Purpose

Sales reps hand-enter deal terms on the Salesforce Opportunity and its Product
Line Items *after* a deal is signed. Those fields flow straight into billing and
renewals, so a typo (wrong minimum, missing opt-out date, un-waived platform fee)
becomes a revenue or churn problem. This audit compares **what the customer
actually signed** (the executed SpotDraft Service Order) against **what was
entered in Salesforce**, and surfaces mismatches in Slack so the deal team can fix
them before they hit billing.

**Two moments we audit:**
1. **Pre-close** — when an opp moves to **Stage 5 (Negotiation & Contracting)** →
   posted in **#sfdc-oppty-audit**. Goal: catch and correct errors *before* the
   deal flips to Closed Won.
2. **Post-close** — when an opp flips to **Closed Won** → posted in
   **#closed-won-presales**. Goal: final verification against the executed SO.

---

## 2. Current architecture (as operated today)

Everything runs **inside Claude Code** as the orchestrator. Claude reads the
`SKILL.md` audit spec, pulls the data from each system, runs the 16 checks, and
posts the result in-thread.

```
                      ┌─────────────────────────────┐
                      │        Claude Code           │
                      │   (reads SKILL.md as spec)   │
                      └───────────────┬─────────────┘
            ┌───────────────┬─────────┼──────────────┬───────────────┐
            ▼               ▼         ▼               ▼               ▼
      Slack MCP      Salesforce    SpotDraft     PDF text         git
   (read + send)     Read-Only      Public API   extractor    (SKILL.md is
   no edit/delete      (SOQL)      (REST/JSON)   (zlib, local)  versioned here)
```

| System | Access | What we use it for |
|---|---|---|
| **Slack MCP** | read + send (no edit, no delete) | Read new alert messages in both channels; post audit replies in-thread. Corrections are **superseding re-posts**, never edits. |
| **Salesforce Read-Only MCP** | SOQL only | Opportunity header + OpportunityLineItem fields; `SpotDraft_Contract__c` records. |
| **SpotDraft Public API** | `client-id` / `client-secret` headers | `GET /public/contracts/{T-id}/key_pointers/` (structured fields) and `POST /public/contracts/{T-id}/download/` (signed PDF). |
| **PDF text extractor** | local zlib script | Read Term dates, fees, and clause wording straight from the SO PDF when `key_pointers` is unreliable or `poppler` is unavailable. |
| **git** | this repo | `SKILL.md` is the single source of truth for the audit logic; updates ship via commit/push. |

**API base:** `https://api.spotdraft.com/api/v2`
**Auth:** header `client-id` + `client-secret` (stored locally in
`spotdraft-auth.json`, **gitignored** — never committed).

---

## 3. Inputs & data model

### Salesforce — Opportunity (header)
`Id, Name, Type, StageName, CloseDate, AccountId, Account.Name, Owner.Name,
Shop_ID__c, Start_Date__c, DocuSign_End_Date__c, Opt_Out__c, Opt_Out_Date__c,
Minimum_Spend__c`

### Salesforce — OpportunityLineItem
`Product2.Name, Package_Type__c, Platform_Fee__c, Number_Of_Months__c, UnitPrice`

### Field gotchas (trust these over labels — learned from the live org)
- **Dates:** use `Start_Date__c` (label "Contract Start Date") and
  `DocuSign_End_Date__c` (label "Contract End Date"). The fields
  `Contract_Start_Date__c` / `Contract_End_Date__c` are **deprecated** — never use.
- **Line-item "Monthly Fee"** is API name `Platform_Fee__c` (label/API mismatch).
  Do **not** use `UnitPrice` for fee checks. Ignore `Calculated_Rate__c`.
- **`Platform_Fee__c` / `Minimum_Spend__c` / `Number_Of_Months__c`** do not exist
  on the Opportunity — `Platform_Fee__c` and `Number_Of_Months__c` live on the
  **line item**; `Minimum_Spend__c` lives on the **Opportunity header**.
- **Plus service window:** derive from line-item `Number_Of_Months__c` (start =
  SO start date, end = start + N months − 1 day). Do **not** compare line-item
  `Start_Date__c`/`End_Date__c` — they auto-stamp from the close date.

### SpotDraft
- `key_pointers` returns a **list** of field objects (`label`, `value`, …) — good
  for Package, Shop name, fee values.
- **`ContractStartDate` in `key_pointers` is unreliable** (observed ~1 month early,
  e.g. Tubby Todd, Dossier). **Always read Term Start/End from the PDF text**, not
  from `key_pointers`.
- `SpotDraft_Contract__c.Opportunity__c` is populated **0 / 630** in 2026 — there is
  **no deterministic opp→contract link** in SFDC today (see §7 contract selection).

---

## 4. Contract selection logic

There is no reliable foreign key from Opportunity to SpotDraft contract, so we
select by heuristic and state our confidence:

1. Prefer the **executed Proposed Service Order** for the account, **created
   nearest** (usually shortly before) the opp CloseDate, typically by the opp owner,
   and belonging to **this** deal.
2. If the deal's own SO exists but is still **in signature stage** (SpotDraft status
   `SIGN` — sent, not yet executed), audit that and mark the result
   **`⚠ PRELIMINARY`**.
3. **Do NOT fall back to a prior-term executed SO** when the current deal has its own
   SO (whether `SIGN` or executed). A renewal/amendment with only a `SIGN` contract
   audits as PRELIMINARY; a deal with only a draft (no published version) is **not
   auditable** and is reported as such.

**Gate checks (report and stop if either fails):**
- Opportunity `StageName` = "Closed Won" (post-close channel) — pre-close channel
  audits Stage 5 opps as PRELIMINARY by design.
- Contract status = Executed or `SIGN`. Earlier stages (draft/negotiation, no
  published version, extraction not completed) fail the gate.

---

## 5. The 16-check audit

Run all checks. A blank/null Salesforce value where the contract has a value is a
**mismatch** ("missing in SFDC"), not a skip.

| # | Check | Contract source | Salesforce source | Pass when |
|---|---|---|---|---|
| 1 | Shop ID | "Shop(s)" row | `Shop_ID__c` | All contract shop IDs present. **New Business + "(0)"/name-only placeholder → ➖ N/A.** Multi-shop split → match on the opp's own shop. |
| 2 | Start Date | SO Term Start (PDF) | `Start_Date__c` | Exact match |
| 3 | End Date | SO Term End (PDF) | `DocuSign_End_Date__c` | Exact match |
| 4 | Opt-out flag | SO-level termination-for-convenience clause (any heading) | `Opt_Out__c` | Present → true; absent → false. **Addendum-level termination rights (e.g. AI 90-day) do NOT count.** |
| 5 | Opt-out date | Effective date of that right | `Opt_Out_Date__c` | Exact match (N/A if no clause & flag correctly false) |
| 6 | Package | SMS Addendum Package | SMS line `Package_Type__c` | Exact (Enterprise / Professional / Growth) |
| 7 | SMS Platform Fee | SMS Platform Fee, **waiver-adjusted** | SMS line `Platform_Fee__c` | Equal. Waived → $0. Blank counts as $0. |
| 8 | Minimum Commitment | Minimum **÷ 3 if quarterly**; **per-shop allocation** if a Minimum Commitment Allocation clause splits across shops | `Minimum_Spend__c` | Equal (show the math) |
| 9 | DSC included | DSC section present? | "Dedicated Short Code" line exists | Both present or both absent |
| 10 | DSC monthly fee | DSC fee, waiver/discount-adjusted | DSC line `Platform_Fee__c` | Equal. Blank = $0 only when waived/$0. |
| 11 | Plus included | Plus Addendum present? | "Postscript Plus" line exists | Both present or both absent |
| 12 | Plus service dates | Plus Addendum Start/End | Window from `Number_Of_Months__c` | Within 1 day = ✅; gap > 1 day = ⚠/mismatch |
| 13 | Plus package | Plus tier (e.g. "Launch", "Signature") | Plus line `Package_Type__c` | Tier matches |
| 14 | Plus monthly fee | Plus Fees ($/mo per shop) | Plus line `Platform_Fee__c` | Equal |
| 15 | AI included | AI Addendum present? | "Postscript AI" line exists | Both present or both absent |
| 16 | AI Platform Fee | AI "Platform Fee Price" ($/mo per shop) | AI line `Platform_Fee__c` | Equal |

### Multi-shop Minimum Commitment allocation (check 8)
When a master SO covers multiple shops and is split into per-shop opportunities,
audit each opp against **its own shop's allocation**, not the aggregate. If the SMS
Addendum has a *Minimum Commitment Allocation* clause listing per-shop amounts, use
the amount for that opp's `Shop_ID__c`, ÷3 for quarterly cadence. Example:
SO total $45,000/qtr → Woolx (6599) $22,500/qtr = **$7,500/mo** → the Woolx opp's
`Minimum_Spend__c` should be $7,500, not the $15,000 aggregate.
(Seen on: Steve Madden 6-shop master, Woolx + Hanks, Dossier + Dossier Mexico.)

### Output format
Lead with a one-line verdict + links, then the full 16-row table (in order), then a
short **"What to fix"** list (one line per ❌/⚠, naming the exact field + correct
value). Show converted-value math (÷3, waiver→$0). PRELIMINARY contracts get the
`⚠ PRELIMINARY` banner directly under the Links line. **Read-only — never writes to
Salesforce or SpotDraft.**

Result tokens: `✅ Match` · `❌ Mismatch` · `❌ Missing in SFDC` ·
`⚠ <note>` · `➖ N/A — not in contract` · `⛔ Blocked (dependent check)`.

---

## 6. Channel routing & per-individual tagging

Two channels, same audit. Who gets @-mentioned depends on the **Opportunity Type**
and whether **Postscript Plus** is on the deal.

| Opportunity Type | Tag |
|---|---|
| Renewal, Upsell | **Caitlin Ferson** |
| New Business, Winback, Captured Account, Amendment | **Lola Gato** |
| *Any of the above* **+ Postscript Plus on the deal** | **+ Viv Hu** |

**Channel-specific override (important):**
- **#closed-won-presales:** full tagging rules above, including **+Viv when PS Plus**.
- **#sfdc-oppty-audit:** **do NOT tag Viv.** Per Viv's in-channel request
  (2026-06-09), she and Lola want to be tagged **only when there is a call to
  action** (a real fix needed), to cut thread noise. Practically: in #sfdc, drop
  Viv entirely; tag the Type owner (Lola/Caitlin) — and lean toward tagging only
  when the audit surfaces something actionable.

User IDs (for the workflow): Caitlin `U077EJVK10R` · Lola `U07GQE3BP7F` ·
Viv `U08CPAGU1DZ`.

---

## 7. Known gotchas & lessons learned (operational playbook)

| # | Gotcha | Handling |
|---|---|---|
| 1 | **Sync lag** — reps post the instant the opp flips, sometimes before the executed SO syncs to SpotDraft. | If no `Completed` contract yet → reply "not yet auditable"; (automation: Wait + retry ~3×). |
| 2 | **`key_pointers.ContractStartDate` is ~1 month early.** | Always read Term dates from the **PDF text**, never from key_pointers. |
| 3 | **`poppler` / Read-PDF unavailable** in some containers. | Use the local zlib PDF text extractors (`/tmp/pdftext.py`, `pdffind.py`, `pdfwin.py`) — decompress FlateDecode streams, regex parenthesized text. |
| 4 | **No deterministic opp→contract link** (`Opportunity__c` 0/630). | Select by newest Completed/SIGN for the account near close (§4); state confidence. |
| 5 | **Wrong-message risk** — auditing the wrong Slack message. | Re-read the channel, confirm exact `ts`, and that the thread has no existing bot reply, before posting. Slack has no delete — mistakes need manual cleanup + a superseding post. |
| 6 | **Stale cached SFDC data.** | Re-pull Salesforce fresh at audit time; don't reuse a prior session's numbers (e.g. Woolx min was already fixed to $7,500 server-side). |
| 7 | **Multi-shop master SO mis-flagged "no contract".** | A shop may be one line on a multi-shop master SO (e.g. Dolce Vita CA on the Steve Madden 6-shop SO) — check the master before declaring "no contract". |
| 8 | **Fondue / Gimme / other products on their own order form.** | Fondue (cashback) typically signs on a **separate order form**, not the main SO. If the opp has a Fondue line but the SO + SpotDraft have no Fondue agreement, flag it ⚠ "confirm separate order form" — don't hard-fail. (Seen on Caden Lane: $4,861.42 Fondue line, no Fondue paperwork in SpotDraft.) |
| 9 | **Plus `Number_Of_Months__c` rounding.** | Reps round the Plus window to whole months, so the derived end can land ~2 weeks short of the SO's month-end (Caden Lane N=12 → 6/14 vs 6/30; Reale N=4 → 10/14 vs 10/31). Flag ⚠, low severity. |
| 10 | **SOQL apostrophes** (e.g. "Y'all"). | Use `LIKE 'Y%all%'` instead of an escaped literal. |
| 11 | **Ramp / temporary minimum discounts.** | A SO may discount the minimum to $0 for an intro period then step to steady-state. The opp's `Minimum_Spend__c` carries the **steady-state** figure — match against that, note the ramp. |

---

## 8. Resolved review comments (Caitlin's doc feedback)

| Theme | Resolution baked into this doc |
|---|---|
| "Selecting by 'newest Completed/Signing' is the riskiest join — validate a deterministic link." | Validated: `SpotDraft_Contract__c.Opportunity__c` is populated **0/630** in 2026, so a deterministic key is unusable today. We keep the heuristic (§4) and **state confidence**; flagged as the #1 reliability risk and a candidate for a future SpotDraft↔SFDC link field. |
| "How is `key_pointers` unreliable?" (plain English) | The structured Start Date field SpotDraft returns runs about a month early vs. the actual signed Term, so we read the date off the signed PDF instead (§3, §7-#2). |
| Pre-close auditing in #sfdc-oppty-audit | Added as a first-class flow (§1, §6) — audit Stage 5 opps as PRELIMINARY to fix before close. |
| Per-individual tagging | Encoded in §6 with the Type→owner table and the PS-Plus→Viv rule. |
| Viv: "only tag if there's a call to action" | §6 channel override — Viv dropped from #sfdc; lean to tagging only on actionable findings. |
| Multi-shop minimums | §5 check-8 allocation rule (per-shop, not aggregate). |

---

## 9. Automation plan (in build)

Goal: run the audit automatically on every new alert in both channels, with no
human in the loop for the happy path.

```
Slack app (event)         n8n (orchestration)              Railway (compute)
─────────────────         ────────────────────             ─────────────────
new message in        →   Slack Trigger → filter      →    Python worker:
#closed-won-presales      (is it a real deal alert?)        • SF SOQL pulls
or #sfdc-oppty-audit      parse name + thread_ts            • contract pick (§4)
                          route to worker                   • SpotDraft kp + PDF
                                                            • 16-check audit
                          ← audit text ←─────────────────   • Claude API (SKILL.md
                          Slack post (in-thread,              as system prompt)
                          tag per §6)
```

**Why two systems:**
- **n8n = orchestration.** It owns the trigger (Slack event), the routing, retries,
  dedupe, and the "post back to the right thread" step. It is the glue, not the brain.
- **Railway = compute.** The Python worker does the heavy, stateful work that doesn't
  belong in n8n nodes: SOQL, contract selection, PDF download + text extraction, and
  the Claude call. Keeping it on Railway means we can iterate on the audit logic in
  real code, log/debug it, and scale it independently.
- **Claude API** runs the actual audit reasoning, using **`SKILL.md` as the system
  prompt** (passed via env var `AUDIT_SKILL` or pasted into the node).

**How SKILL.md updates flow:** `SKILL.md` lives in this git repo and is the single
source of truth. Update it → commit → push. The worker reads the latest `SKILL.md`
(from the repo at deploy, or synced into the `AUDIT_SKILL` env var), so a logic
change ships by editing one file — no workflow rewiring.

**Built artifacts (in repo):**
- `n8n-closed-won-audit.workflow.json` — importable workflow (Slack Trigger → filter
  → parse → SF token/queries → contract pick → SpotDraft kp + PDF → Claude → Slack
  post; secrets via `$env.*`).
- `n8n-closed-won-audit.GUIDE.md` — setup, credentials/env vars, and the two
  built-in gotchas (sync-lag retry, dedupe).

**Two gotchas the automation must handle:**
1. **Sync lag** — branch to "not yet auditable" when no executed SO exists; optional
   Wait + retry (~10 min, ≤3 tries) before giving up.
2. **Dedupe** — before posting, check the thread for an existing bot reply (or store
   handled `ts`) so a re-fired event doesn't double-post.

---

## 10. Roadmap / open items

| Priority | Item |
|---|---|
| P0 | Stand up the n8n workflow + Railway worker for **both** channels; activate dedupe + sync-lag retry. |
| P0 | Encode §6 tagging (incl. #sfdc no-Viv + CTA-only) in the worker. |
| P1 | Pursue a **deterministic opp→contract link** (SpotDraft contract ID on the opp, or `Opportunity__c` populated) to retire the "newest contract" heuristic. |
| P1 | Add Fondue / Gimme separate-order-form lookup so those products audit cleanly instead of flagging ⚠. |
| P2 | Decide whether Lola is tagged on every New Business audit or only on actionable findings (pending Caitlin/Lola confirmation). |
| P2 | Capture a per-shop allocation table automatically for multi-shop masters. |

---

## 11. Guardrails (non-negotiable)

- **Read-only** on Salesforce and SpotDraft — the audit never writes.
- **No Slack edit/delete** — corrections are superseding re-posts.
- **Secrets stay local + gitignored** (`spotdraft-auth.json`, `sf-auth.json`) — never
  committed. Workflow reads them from env vars.
- **Confidence stated** whenever contract selection is heuristic; PRELIMINARY banner
  on any unexecuted (`SIGN`) contract.
