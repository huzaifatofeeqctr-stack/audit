---
name: closed-won-contract-audit
description: >-
  Audit a Salesforce Closed Won Opportunity against its executed (or
  signature-stage) SpotDraft Service Order to catch data-entry mismatches
  before they hit billing. Trigger on any request to compare contracted
  Service Order terms (dates, fees, package, minimum commitment, DSC, Plus,
  AI addendums, opt-out) to what was entered on the Opportunity and its
  Product Line Items.
---

# Closed Won Opportunity vs. SpotDraft Contract Audit

Compare what the customer actually signed (the executed SpotDraft Service
Order) to what was entered in Salesforce on the Closed Won Opportunity and its
Product Line Items. Sales reps enter these fields by hand after a deal closes,
and mistakes flow directly into billing and renewals — that's why every check
below matters.

The audit runs on **one Opportunity at a time** and the deliverable is a
**results table in chat** (no file output unless asked).

## Inputs

The user provides some combination of: an Opportunity name/link/Id, an account
name, a SpotDraft contract link, or a contract PDF. Whatever is missing, find:

1. **Opportunity**: query Salesforce (Salesforce MCP `soqlQuery` / `find`).
   If multiple Closed Won opps exist for the account, prefer the one whose
   CloseDate is nearest the contract execution date, and confirm the choice
   with the user before auditing.
2. **Contract**: use the SpotDraft MCP (`lookup_account` /
   `lookup_opportunity` / `get_contract_list` to locate it,
   `get_contract_content` or `get_contract_key_pointers` to read it). If the
   user uploaded a PDF instead, extract text from the PDF directly.
3. **Contract selection**: prefer the executed governing contract belonging to
   this deal (created nearest — usually shortly before — the opp CloseDate,
   typically by the opp owner). The governing contract is whichever of these is
   newest for the deal: a **Service Order** (New Business / Renewal) **or a
   Statement of Work / Contract Addendum** (**Upsell / Amendment**). An
   Upsell/Amendment is papered by a **SOW / Contract Addendum attached to that
   upsell** — audit against **that** SOW, NOT the prior base Service Order. Using
   the older base SO pulls stale dates/terms (e.g. the base SO's start date
   instead of the upsell's). If the deal's contract is still in signature stage
   (SpotDraft "SIGN"), audit that in-progress contract and mark the result
   PRELIMINARY. Do NOT fall back to a prior-term executed SO when the current
   deal has its own Service Order or SOW (SIGN or EXECUTED).

### Auto-renewal deals (Renewal opps with no newly signed SO)

Some Renewal opps reach Closed Won by **auto-renewing** a prior contract that
contained auto-renewal language — no new SO is signed. The deterministic signal
is `Closed_Won_Reason__c = "Auto-Renewal"`, but **that flag alone is not
reliable** (reps sometimes mark it on deals that actually have a new SO, or whose
prior SO does not auto-renew). Resolve the governing contract with this order:

- **(a) A newer EXECUTED (Completed) SO exists for the account/shop** covering
  the new term → audit against it normally. It's a freshly signed renewal; the
  "Auto-Renewal" reason is cosmetic. *(Not really an auto-renewal.)*
- **(b) A renewal SO is in Signing (SIGN)** → audit against it, mark
  **PRELIMINARY**. The renewal is being (re)papered, not auto-renewed.
- **(c) No new SO at all** → it's a **true auto-renewal**. The binding contract
  is the **most recent executed SO**, carried into the new term by its
  auto-renewal clause. This is the **one explicit exception** to "no prior-term
  fallback." Audit the opp against that prior SO and lead the output with:
  `🔁 AUTO-RENEWAL — no new SO; audited against prior-term SO <T-id> (auto-renewed). Pricing/terms carry forward.`

For a true auto-renewal (case c):
- **Run the full 16-check audit anyway.** "Carry-forward" does not mean
  "minimum-only." An auto-renewal still gets all 16 checks against the prior SO,
  and **every** mismatch is reported — most commonly a **waived Platform Fee**
  (the SO waives it → expected SF `Platform_Fee__c` = $0; a non-zero line item
  like $100 is a ❌, even when the minimum is also wrong). Finding the minimum
  error does not excuse skipping checks 6–16. A single deal can carry two or
  more independent errors at once.
- **Confirm the SO actually auto-renews** — its Renewal row reads "will renew
  for additional, successive N-month renewal terms." If it reads "will **NOT**
  automatically renew," flag ⚠ — the auto-renewal is contractually unsupported
  and a new SO is required (look for one in Signing).
- **Dates (checks 2–3) roll forward:** opp `Start_Date__c` should equal prior SO
  End + 1 day; opp `DocuSign_End_Date__c` should equal that Start + the renewal
  term − 1 day (usually +12 months). Pass when they roll correctly; the literal
  prior-SO dates will NOT match, so compare against the rolled-forward term.
- **Carry-forward (checks 6–16):** package, SMS/DSC/Plus/AI fees, and minimum
  must equal the prior SO — auto-renewal renews on the **same terms**. A changed
  minimum, package, or an added product (e.g. a new AI/Plus line not in the
  renewing SO) is a ❌/⚠: it requires a signed amendment, not just a renewal.
  **`Minimum_Spend__c = $0` is only a flag if the renewing SO has a real
  Minimum Commitment.** Many legacy/SMS SOs list "Minimum Commitment Amount:
  N/A" (or have no minimum section) — there `$0` is ✅ correct, not a miss. Read
  the SO before flagging a $0 minimum.

**Gate checks before auditing** (report and stop if either fails):
- Opportunity `StageName` = "Closed Won"
- Contract status = Executed or in signature stage ("SIGN"). A SIGN-status
  contract is auditable via `get_contract_content` like any other, but the
  audit is preliminary: lead the output with
  `⚠ PRELIMINARY — contract in signature stage, not yet executed; terms may change before signing. Re-audit after execution.`
  Contracts in earlier stages (draft/negotiation with no published version, or
  extraction_status not "completed") fail this gate.
  **Exception:** a true auto-renewal (above, case c) has no new contract — it
  does not fail this gate; audit it against the auto-renewing prior SO.

## Reading the contract

A Postscript Service Order has a main **Postscript Service Order section**
plus optional addendums: **SMS Marketing Addendum**, **Postscript Plus
Addendum**, **Postscript AI Addendum**. Pull these values:

| Contract field | Where it lives |
|---|---|
| Shop ID(s) | "Shop(s)" row — the number in parentheses, e.g. `LOOK OPTIC (892108)` |
| Start Date / End Date | "Term" rows of the main Service Order section (NOT addendum dates) |
| Opt-out language | Any Service Order-level **termination-for-convenience clause**, regardless of heading — seen titled "One-Time Termination Right", "Mid-Term Termination Right", and similar. What matters is the substance: the customer may terminate without breach, effective on a stated date. Termination rights inside an addendum (e.g. the AI Addendum's 90-day right) do NOT count. |
| Opt-out date | The **effective date** of that termination right (e.g. "effective as of September 30, 2026" → 2026-09-30), not the notice deadline |
| Package | SMS Marketing Addendum "Package" row (Enterprise / Professional / Growth) |
| SMS Platform Fee + waiver | SMS Marketing Addendum "Platform Fees" row; note any "Platform Fee Waiver" |
| Minimum Commitment + frequency | SMS Marketing Addendum "Minimum Commitment" section — note whether monthly or quarterly |
| DSC fee + waiver | SMS Marketing Addendum "Dedicated Short Code" section |
| Plus: dates, package, fee | Postscript Plus Addendum. Package tier is one of Essentials / Signature / Launch (the addendum may prefix "Plus", e.g. "Plus Launch" → compare only the tier word "Launch"; the SFDC value is the bare tier with no "Plus") |
| AI Platform Fee | Postscript AI Addendum "AI Platform Fee Price" row (per month per Shop) |

## Reading Salesforce

Two queries cover everything. Use the real Opportunity Id.

```sql
SELECT Id, Name, Type, StageName, CloseDate, AccountId, Account.Name,
       Owner.Name, Shop_ID__c, Start_Date__c, DocuSign_End_Date__c,
       Opt_Out__c, Opt_Out_Date__c, Minimum_Spend__c
FROM Opportunity WHERE Id = '<OPP_ID>'
```

```sql
SELECT Id, Product2.Name, Package_Type__c, Platform_Fee__c,
       Monthly_Fee_Minimum__c, Number_Of_Months__c, UnitPrice
FROM OpportunityLineItem WHERE OpportunityId = '<OPP_ID>'
```

Field gotchas learned from the live org — trust these over labels:
- **Dates**: use `Start_Date__c` (label "Contract Start Date") and
  `DocuSign_End_Date__c` (label "Contract End Date"). The fields named
  `Contract_Start_Date__c` / `Contract_End_Date__c` are deprecated — never
  use them.
- **Line item "Monthly Fee"** is API name `Platform_Fee__c` (label/API
  mismatch).
- **Ignore `Calculated_Rate__c`** on line items entirely — out of scope for
  fee checks. Also do not use `UnitPrice` for fee checks; the contracted
  monthly fee lives in `Platform_Fee__c`.
- **Do not compare line-item `Start_Date__c`/`End_Date__c` to contract
  dates** — those auto-stamp from the opp close date. The rep-entered
  service-length field is `Number_Of_Months__c`; derive the service window
  from it (see check 12).
- Line items are identified by `Product2.Name`: "SMS Marketing",
  "Dedicated Short Code", "Postscript Plus", "Postscript AI" (plus others
  like "Shopper", "IT Campaigns", "IT Automations" that this audit does not
  check).

## The checks

Run all of these. A blank/null Salesforce value where the contract has a
value is a **mismatch** (report as "missing in SFDC"), not a skip.

**Never flag a value that matches.** A check is `mismatch`/`warning` ONLY when
the contract value and the Salesforce value genuinely differ (for money, differ
by more than $5). If they agree, the status is `match` and the check produces
**no** `fix` and **no** Updates Required bullet. Do not emit a "fix" that
restates the same value (e.g. `$1,250 → $1,250`, `$699 → $699`, "value correct",
"no change", "matches", "OK") — if there is nothing to change, it is a match,
full stop. A deal with zero genuine differences must report **clean**.

**Do not hallucinate Plus (or any product) inclusion.** Decide Plus included
(check 11) strictly from an actual **Postscript Plus Addendum section** in the
contract PDF **and** a "Postscript Plus" SFDC line item. A populated key-pointer
template field is not evidence. If both are absent, checks 11–14 are ✅/N-A
(correctly absent) — never invent a Plus mismatch.

**Monetary tolerance (±$5).** For every check that compares a **dollar amount**
— SMS Platform Fee (7), Minimum Commitment (8), DSC fee (10), Plus fee (14),
AI Platform Fee (16) — treat the values as a **✅ Match when they are within
$5 of each other** (`|contract − SFDC| ≤ $5`). Only a gap **greater than $5**
is a ❌ Mismatch. A cent-level or few-dollar rounding difference is not worth
flagging. (If you want, append "(within $5)" to the cell, but it still counts
as ✅, not a warning.) This tolerance applies to money only — dates, packages,
shop IDs, and product inclusion remain exact.

| # | Check | Contract value | Salesforce value | Pass when |
|---|---|---|---|---|
| 1 | Shop ID | Shop ID(s) from "Shop(s)" row | Opportunity `Shop_ID__c` | Every contract Shop ID appears (exact string match per ID). **New Business exception:** if the contract has no usable Shop ID (name-only row or a "(0)" placeholder) and the Opportunity `Type` is "New Business", mark ➖ N/A. For any other Type, a missing/unverifiable Shop ID is still a ⚠. **Multi-shop:** when a multi-shop SO is split into per-shop opportunities, the opp matches on its own `Shop_ID__c`; note the other shops are covered under their own opps/allocations rather than hard-failing the missing IDs |
| 2 | Contract Start Date | Service Order Term Start Date | Opportunity `Start_Date__c` | Exact date match |
| 3 | Contract End Date | Service Order Term End Date | Opportunity `DocuSign_End_Date__c` | Exact date match |
| 4 | Opt-out flag | Service Order-level termination-for-convenience clause exists (any heading)? | Opportunity `Opt_Out__c` | Language present → `true`; absent → `false` |
| 5 | Opt-out date | Effective date of the termination right | Opportunity `Opt_Out_Date__c` | Exact date match (N/A if no opt-out language and flag is correctly false) |
| 6 | Package type | SMS Addendum Package | SMS Marketing line item `Package_Type__c` | Exact picklist match |
| 7 | SMS Platform Fee | SMS Addendum Platform Fee, **waiver-adjusted** | SMS Marketing line item `Platform_Fee__c` | Amounts within $5 (±$5 tolerance). **Waiver rule:** if waived, expected SF value is $0. **For this check only, blank/null `Platform_Fee__c` counts as $0** — blank = ✅ when contract fee is waived or $0 |
| 8 | Minimum Commitment | Minimum Commitment **converted to monthly** (quarterly ÷ 3). **Multi-shop allocation:** if the SMS Addendum has a *Minimum Commitment Allocation* clause splitting the total across Shop IDs (e.g. `Woolx: $22,500.00; Hanks Leather Goods (73146): $22,500.00`), use the amount allocated to **this opp's `Shop_ID__c`** (the shop the opportunity represents), converted to monthly — NOT the aggregate total | Opportunity `Minimum_Spend__c` | Amounts within $5 (±$5 tolerance) (show the allocation + the ÷3 math) |
| 9 | DSC included | SMS Addendum has a Dedicated Short Code section with DSC fee? | "Dedicated Short Code" line item exists | Both present or both absent |
| 10 | DSC monthly fee | DSC fee, **waiver-adjusted** (waived → $0) | DSC line item `Platform_Fee__c` | Amounts within $5 (±$5 tolerance). **Blank/null counts as $0** — blank = ✅ when waived or $0 (blank against a real discounted fee like $200 is still a mismatch) |
| 11 | Plus included | Postscript Plus Addendum present? | "Postscript Plus" line item exists | Both present or both absent |
| 12 | Plus service dates | Plus Addendum Start/End Dates | Window from Plus line item `Number_Of_Months__c`: start = Service Order Start Date, end = start + N months − 1 day | Derived window equals Plus Addendum dates. **Mid-month tolerance:** within 1 day = ✅ (note it); gap >1 day is a real mismatch |
| 13 | Plus package | Plus Addendum package tier | Plus line item `Package_Type__c` | Tier matches. **The SFDC `Package_Type__c` for a Plus line is the bare tier — exactly one of `Essentials`, `Signature`, or `Launch` — with NO "Plus" prefix.** The contract may write it as "Plus Signature" / "Plus Launch"; compare only the tier word. So contract "Plus Signature" vs SFDC "Signature" is a ✅ Match, NOT a mismatch. Never expect or suggest "Plus Signature"/"Plus Essentials"/"Plus Launch" as the SFDC value |
| 14 | Plus monthly fee | Plus Addendum Fees ($/mo per Shop) | Plus line item `Platform_Fee__c` | Amounts within $5 (±$5 tolerance) |
| 15 | AI included | Postscript AI Addendum present? | "Postscript AI" line item exists | Both present or both absent |
| 16 | AI Platform Fee | AI Addendum "AI Platform Fee Price" ($/mo per Shop) | AI line item `Platform_Fee__c` — **ignore `Calculated_Rate__c` and `UnitPrice`** | Amounts within $5 (±$5 tolerance) |

Conditional checks (5, 10, 12–14, 16) become **N/A — not in contract** when
the underlying addendum/section is absent, as long as the corresponding
"included" check passed. If an addendum is missing where the contract has one
(or vice versa), report the inclusion mismatch and mark the dependent checks
"blocked".

Multi-shop contracts: per-Shop fees ($/mo per Shop) are expected to be
multiplied by the number of shops only if the line item represents all shops —
if amounts differ by an exact shop-count multiple, flag it as a ⚠ with a note
rather than a hard fail, and say why.

**Multi-shop Minimum Commitment allocation (check 8):** when the SMS Addendum
includes a *Minimum Commitment Allocation* clause listing per-Shop-ID amounts,
the aggregate total is NOT the figure to audit. Each shop's opportunity should
carry that shop's allocated minimum (÷3 for quarterly cadence). Compare the
opp's `Minimum_Spend__c` to the allocation matching the opp's own `Shop_ID__c`,
and show the allocation breakdown in the result so the reviewer can see the
split. Example: SO total $45,000/qtr → Woolx (6599) $22,500/qtr = $7,500/mo;
the Woolx opp's `Minimum_Spend__c` should be $7,500, not the $15,000 aggregate.

## Output format

Keep it short and scannable — Sales Ops asked for this exact shape. **Do not
dump a 16-row table.** Lead with the opportunity, links, owner, and a one-line
result, then an **Updates Required** bullet list (one line per fix):

```
Audit: <Opportunity Name> vs. <Contract Name>
Links: [Opportunity](https://postscript.lightning.force.com/lightning/r/Opportunity/<OPP_ID>/view) · [Account](https://postscript.lightning.force.com/lightning/r/Account/<ACCOUNT_ID>/view) · [SpotDraft Contract](https://app.spotdraft.com/contracts/v2/<numeric id>)
Owner: <Opportunity Owner.Name>
Result: 14 of 16 checks passed — 2 mismatches

Updates Required:
• Postscript AI `Platform_Fee__c`: $599 → $199
• `Minimum_Spend__c`: $6,333.33 → $5,000
```

When the audited contract is in signature stage (SIGN), insert
`⚠ PRELIMINARY — contract not yet executed; terms may change before signing. Re-audit after execution.`
directly under the Owner line.

When the deal is clean: `Result: 16 of 16 checks passed — clean ✅` then
`✅ Clean — nothing to fix.` (with at most one short note if context helps).

Still evaluate **all 16 checks** internally. Every ❌ mismatch and ⚠ actionable
warning must appear as its **own** Updates Required bullet — never drop the
second/third finding (status values: match, mismatch, missing_sfdc, warning,
na, blocked). Each bullet is one terse line naming the exact Salesforce
field/line item and its corrected value (`current → corrected`); show a
converted value only when needed (quarterly ÷ 3, waiver → $0), and no
explanatory sentences. **When a line item is missing in SFDC, always state the
fee/value to enter from the contract** — e.g. `Add Postscript AI line item —
Platform_Fee $99` or `Add Dedicated Short Code line item — $250`, never a bare
"add X line item". Do not write to Salesforce or SpotDraft — read-only.

**Slack tagging — tag only on a call to action.** When posting the audit to
Slack, a **clean result (all applicable checks ✅, 0 mismatches and 0 warnings)
gets NO @-mention** — the table plus a ✅ reaction on the alert is the signal.
Only @-mention the owner when there is something actionable (a ❌ mismatch, a
⚠ warning, or "not yet auditable"). Routing when there *is* a finding: Renewal /
Upsell → Caitlin; New Business / Winback / Captured Account / Amendment → Lola;
+ Viv if the deal has Postscript Plus. In **#sfdc-oppty-audit** never tag Viv.

## Batch runs

**Every opportunity in a batch or wave gets the full 16-check audit — no
shortcuts.** Running fast (e.g. a 5-minute wave cadence) does not reduce the
checks. Reporting only the minimum (or any single headline check) and dropping
the other 15 is a defect: it lets a second mismatch on the same deal — a waived
Platform Fee entered as $100, a wrong package, a missing DSC — go unreported.
Each deal's reporting must surface **all** of its mismatches and warnings.

Lead the report with a summary table using exactly these columns — the
Opportunity cell links to Salesforce and the Contract cell links to SpotDraft.
The **Mismatches** cell must list **every** ❌/⚠ for that deal, not just one:

```
| Opportunity | Owner | Contract | Passed | Mismatches |
|---|---|---|---|---|
| [Acme \| NB 1-2026](https://postscript.lightning.force.com/lightning/r/Opportunity/<OPP_ID>/view) | Jane Rep | [T-12345](https://app.spotdraft.com/contracts/v2/12345) | 13/16 | Min $1 s/b $500; SMS platform fee $100 s/b $0 (waived); start date |
```

Then include the **full per-opp audit block** (the concise Output-format block
above — Result line + every Updates Required bullet) for **every** deal that has
any ❌ or ⚠ — not a one-line verdict. A deal may be summarized in one line in the
table only when it is fully clean (all applicable checks ✅). The per-wave
"Fixes" list must enumerate every field to correct across all deals, one line
per mismatch (e.g. "Allegory: `Minimum_Spend__c` $500 → $2,000; SMS
`Platform_Fee__c` $100 → $0"), so nothing actionable is hidden behind a single
headline.
