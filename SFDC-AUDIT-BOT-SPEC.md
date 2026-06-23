# SFDC Oppty Audit Bot — Operating Spec & Monitoring Runbook

**Purpose:** define exactly how the audit bot ("Postscript Audit Agent") is supposed to behave, so anyone can tell at a glance whether it's working as intended. If a posted audit doesn't match this doc, that's a bug.

**Bot identity:** Postscript Audit Agent (`U0B8RPYQ2GL`) · **Worker:** `audit-production-54bd.up.railway.app` · **Channels:** `#closed-won-presales` (`C08JS7N86D6`), `#sfdc-oppty-audit` (`C0B88KMFJ3E`).

---

## 1. What triggers an audit

| | |
|---|---|
| **Source** | A **Rattle** post (sender `U05AA8MBV9B`) in either watched channel that contains a `*Name:*` line. |
| **#closed-won-presales** | "just closed …" → opp is **Closed Won** → **full audit**. |
| **#sfdc-oppty-audit** | "just moved … to Stage 5 / Pricing & Negotiations" → opp is **pre-close** → audit marked **⚠ PRELIMINARY**. |
| **Ignored** | Anything not from Rattle; Rattle posts with no `*Name:*` line (celebration-only); the bot's own replies; message edits/deletes. |
| **One audit per alert** | Each Rattle alert gets **exactly one** threaded reply. Re-runs must not double-post (dedup on the thread already having a bot reply). |

> Note: deal fields live in the Rattle message **blocks**, not the top-level `text`. The trigger/parse must read blocks (this is a known gotcha).

---

## 2. Timing / SLA

| Metric | Target | Notes |
|---|---|---|
| Trigger latency | near-instant | Slack Events API **pushes** to n8n the moment Rattle posts. |
| Audit posted in-thread | **within ~2 min** of the Rattle post | Worker does SF + SpotDraft PDF + Claude (~20–40 s typical). |
| Transient failure retry | 3 tries, 5 s backoff | Absorbs Railway cold-start 502s. |
| "Not yet auditable" (SO not synced yet) | same ~2 min window | Posts the not-auditable note immediately; does **not** silently wait. |

**If no reply appears within ~5 minutes → something is down** (see §7 red flags).

---

## 3. Message format (exact)

A normal audit reply, **threaded under the Rattle alert**, looks like this (Slack-rendered):

```
*Audit: <Opp Name> vs. <Contract Name> (T-#####)*
*Links:* <Opportunity> · <Account> · <SpotDraft Contract>      ← clickable
*Owner:* <Owner Name>
[⚠ PRELIMINARY — contract in signature stage … (only if SIGN)]
*Result: X of 16 checks passed — N mismatches, M warnings*

```​
#   Check                Contract            Salesforce          Result
1   Shop ID              …                   …                   ✅ Match
2   Contract Start Date  2026-07-01          2026-07-01          ✅ Match
… all 16 rows, aligned, monospace …
```​

*What to fix:*
• <field/line item> <current> → <correct value>   (one bullet per ❌)

cc <@owner>           ← only when there is a finding
```

**Format rules (each is checkable):**
- Title, "Links:", "Owner:", "Result:" lines are **bold** (`*…*`), not `**…**` or `##`.
- Links are **clickable** `<url|label>` (never raw `[label](url)` or `<OPP_ID>` placeholders).
- The 16-check table is an **aligned monospace code block** (```), not raw `|` pipes. Result cells use ✅ / ❌ / ⚠ / ➖ / ⛔.
- `What to fix` appears **only when there are ❌ mismatches**; each bullet names the exact SFDC field + the value it should be, with any conversion math shown (e.g. quarterly ÷ 3).
- **Header counts must equal the table:** `X = ✅+➖ rows`, `N = ❌ rows`, `M = ⚠ rows`. (These are computed in code from the table — they should always agree.)

**Result tokens:** `✅ Match` · `❌ Mismatch` · `❌ Missing in SFDC` · `⚠ <note>` · `➖ N/A — not in contract` · `⛔ Blocked (dependent check)`.

**Not-auditable variants** (no table) — title reads e.g.:
- `⚠ Not yet auditable (no SO synced)` / `(SO still in Draft)` / `(amendment SO not synced)`
- `Could not find an auditable Opportunity named *…*`
- `➖ Outside the SMS 16-check audit (Fondue-only deal)`

---

## 4. The 16 checks (what's being compared)

| # | Check | Pass when |
|---|---|---|
| 1 | Shop ID | Contract Shop ID(s) appear in opp `Shop_ID__c` (NB w/ name-only SO row → ➖ N/A) |
| 2 | Contract Start Date | SO Term Start = `Start_Date__c` |
| 3 | Contract End Date | SO Term End = `DocuSign_End_Date__c` |
| 4 | Opt-out flag | SO-level termination-for-convenience present ↔ `Opt_Out__c` (AI 90-day trial right does NOT count) |
| 5 | Opt-out date | effective date = `Opt_Out_Date__c` |
| 6 | Package | SMS Addendum package = SMS line `Package_Type__c` |
| 7 | SMS Platform Fee | waiver-adjusted ($ waived → $0) = `Platform_Fee__c` |
| 8 | Minimum Commitment | monthly (quarterly ÷ 3); **multi-shop → this shop's allocation** = `Minimum_Spend__c` |
| 9–10 | DSC included / fee | presence + waiver-adjusted fee |
| 11–14 | Plus included / dates / package / fee | dates derived from `Number_Of_Months__c` (start = SO start, end = +N months − 1 day) |
| 15–16 | AI included / Platform Fee | presence + `Platform_Fee__c` (ignore `UnitPrice`/`Calculated_Rate__c`) |

**Auto-renewal (Renewal, no new SO):** audited against the auto-renewing prior-term SO; carry-forward terms; the one exception to "no prior-term fallback."

---

## 5. Routing / tagging (must be exact)

| Situation | @-mention | ✅ reaction on the Rattle alert |
|---|---|---|
| **Clean** (all applicable ✅, 0 ❌ / 0 ⚠) | **none** | **yes** |
| Renewal / Upsell finding | **Caitlin** `U077EJVK10R` | no |
| New Business / Winback / Captured Account / Amendment finding | **Lola** `U07GQE3BP7F` | no |
| …and the deal has **Postscript Plus** | **+ Viv** `U08CPAGU1DZ` | no |
| **In `#sfdc-oppty-audit`** | **never tag Viv** (even with Plus) | — |
| Not-auditable / needs-action notes | route by Type (as above) | no |

---

## 6. Idempotency, freshness, read-only

- **One reply per thread.** Before posting, the worker checks the thread for an existing bot reply (`SLACK_BOT_USER_ID`) and skips if present.
- **Always re-pulls SFDC live** at audit time (never audits stale cached data).
- **Read-only:** the bot never writes to Salesforce or SpotDraft. Slack has **no edit/delete** — corrections are superseding re-posts in the same thread.
- **Term dates come from the SO PDF text**, not `key_pointers` (those run ~1 month early).
- **Placeholder `key_pointers` are ignored** — product inclusion needs a real addendum in the PDF **and** the matching SFDC line item.

---

## 7. How to monitor — per-audit checklist

For any Rattle alert, confirm:

- [ ] **Replied within ~2 min**, **in-thread**, **exactly once**.
- [ ] **Renders** (bold title, clickable links, monospace table) — *no raw `##`/`**`/`[]()`/`|`*.
- [ ] **Header counts match the table** (passed / mismatches / warnings).
- [ ] **Links use real IDs** (no `<OPP_ID>`/`<ACCOUNT_ID>` placeholders).
- [ ] **Tagging correct:** clean → no tag **and** ✅ reaction; finding → right owner; **no Viv in #sfdc**.
- [ ] **PRELIMINARY banner** present iff the contract is SIGN/Signing (pre-close).
- [ ] **Spot-check 2–3 cells** against the SO PDF / SFDC (esp. minimum math, package, fees).
- [ ] **"What to fix"** lists every ❌ with the exact field + correct value.

### Green signals (working as intended)
- Clean deals get a ✅ on the alert and no @-mention.
- Mismatches name a specific SFDC field to change.
- Pre-close deals say PRELIMINARY; deals with no/draft SO say "not yet auditable."

### 🔴 Red flags (investigate)
| Symptom | Likely cause |
|---|---|
| No reply after ~5 min | n8n trigger not firing / worker down / Slack request_url misrouted |
| Raw `##`, `**`, `[](url)`, `|` pipes in the post | rendering regression (worker `_to_slack`) |
| "Could not find an auditable Opportunity" on a real deal | opp renamed since the alert, or SF record-visibility (Spotdraft Bot `View All`) |
| Clean deal got an @-mention, or Viv tagged in #sfdc | tagging regression |
| Two audit replies on one alert | dedup off (`SLACK_BOT_USER_ID` unset) |
| Header says "3 mismatches" but table shows 2 | (should be impossible — counts are code-derived; if seen, post-processing broke) |
| #sfdc never audits but #closed does | single Slack request_url pointed at one channel-scoped trigger (use one whole-workspace trigger) |

### Health checks (anytime)
- `GET /health` → `{ ok:true, version:"0.5.x" }` (confirms deploy is live).
- `GET /whoami` → run-as Salesforce user (`Spotdraft Bot`) + masked client-id (confirms SF identity).
- **Weekly smoke test:** POST `/audit` with `{ "dry_run": true, "text": "*Name:* <a known recent deal>" }` and eyeball the rendered output — `dry_run` returns the message and posts nothing.

---

## 8. Known limitations (expected, not bugs)
- **Multi-shop SO synced under a different account** (e.g. Branch CA under Branch) → "no contract found" in Path A.
- **Fondue / Gimme** sign on separate order forms → flagged "confirm separate order form," not audited.
- **Plus `Number_Of_Months__c` rounding** → derived end can land ~2 weeks short of month-end → ⚠ low-severity.
- **Model variance** on borderline ⚠ judgment calls; the **verdict, counts, and tagging are deterministic** (code), so those don't vary.
