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
3. **Contract selection**: prefer the executed Proposed Service Order
   belonging to this deal (created nearest — usually shortly before — the opp
   CloseDate, typically by the opp owner). If the deal's SO exists but is still
   in signature stage (SpotDraft contract_status "SIGN" — sent for
   signature, not yet fully executed), audit that in-progress contract instead
   and mark the result PRELIMINARY. Do NOT fall back to a prior-term
   executed SO when the current deal has its own SO (SIGN or EXECUTED).

**Gate checks before auditing** (report and stop if either fails):
- Opportunity `StageName` = "Closed Won"
- Contract status = Executed or in signature stage ("SIGN"). A SIGN-status
  contract is auditable via `get_contract_content` like any other, but the
  audit is preliminary: lead the output with
  `⚠ PRELIMINARY — contract in signature stage, not yet executed; terms may change before signing. Re-audit after execution.`
  Contracts in earlier stages (draft/negotiation with no published version, or
  extraction_status not "completed") fail this gate.

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
| Plus: dates, package, fee | Postscript Plus Addendum (package e.g. "Plus Launch" → compare the tier word, "Launch") |
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

| # | Check | Contract value | Salesforce value | Pass when |
|---|---|---|---|---|
| 1 | Shop ID | Shop ID(s) from "Shop(s)" row | Opportunity `Shop_ID__c` | Every contract Shop ID appears (exact string match per ID). **New Business exception:** if the contract has no usable Shop ID (name-only row or a "(0)" placeholder) and the Opportunity `Type` is "New Business", mark ➖ N/A. For any other Type, a missing/unverifiable Shop ID is still a ⚠. **Multi-shop:** when a multi-shop SO is split into per-shop opportunities, the opp matches on its own `Shop_ID__c`; note the other shops are covered under their own opps/allocations rather than hard-failing the missing IDs |
| 2 | Contract Start Date | Service Order Term Start Date | Opportunity `Start_Date__c` | Exact date match |
| 3 | Contract End Date | Service Order Term End Date | Opportunity `DocuSign_End_Date__c` | Exact date match |
| 4 | Opt-out flag | Service Order-level termination-for-convenience clause exists (any heading)? | Opportunity `Opt_Out__c` | Language present → `true`; absent → `false` |
| 5 | Opt-out date | Effective date of the termination right | Opportunity `Opt_Out_Date__c` | Exact date match (N/A if no opt-out language and flag is correctly false) |
| 6 | Package type | SMS Addendum Package | SMS Marketing line item `Package_Type__c` | Exact picklist match |
| 7 | SMS Platform Fee | SMS Addendum Platform Fee, **waiver-adjusted** | SMS Marketing line item `Platform_Fee__c` | Amounts equal. **Waiver rule:** if waived, expected SF value is $0. **For this check only, blank/null `Platform_Fee__c` counts as $0** — blank = ✅ when contract fee is waived or $0 |
| 8 | Minimum Commitment | Minimum Commitment **converted to monthly** (quarterly ÷ 3). **Multi-shop allocation:** if the SMS Addendum has a *Minimum Commitment Allocation* clause splitting the total across Shop IDs (e.g. `Woolx: $22,500.00; Hanks Leather Goods (73146): $22,500.00`), use the amount allocated to **this opp's `Shop_ID__c`** (the shop the opportunity represents), converted to monthly — NOT the aggregate total | Opportunity `Minimum_Spend__c` | Amounts equal (show the allocation + the ÷3 math) |
| 9 | DSC included | SMS Addendum has a Dedicated Short Code section with DSC fee? | "Dedicated Short Code" line item exists | Both present or both absent |
| 10 | DSC monthly fee | DSC fee, **waiver-adjusted** (waived → $0) | DSC line item `Platform_Fee__c` | Amounts equal. **Blank/null counts as $0** — blank = ✅ when waived or $0 (blank against a real discounted fee like $200 is still a mismatch) |
| 11 | Plus included | Postscript Plus Addendum present? | "Postscript Plus" line item exists | Both present or both absent |
| 12 | Plus service dates | Plus Addendum Start/End Dates | Window from Plus line item `Number_Of_Months__c`: start = Service Order Start Date, end = start + N months − 1 day | Derived window equals Plus Addendum dates. **Mid-month tolerance:** within 1 day = ✅ (note it); gap >1 day is a real mismatch |
| 13 | Plus package | Plus Addendum package tier (e.g. "Plus Launch" → "Launch") | Plus line item `Package_Type__c` | Tier matches |
| 14 | Plus monthly fee | Plus Addendum Fees ($/mo per Shop) | Plus line item `Platform_Fee__c` | Amounts equal |
| 15 | AI included | Postscript AI Addendum present? | "Postscript AI" line item exists | Both present or both absent |
| 16 | AI Platform Fee | AI Addendum "AI Platform Fee Price" ($/mo per Shop) | AI line item `Platform_Fee__c` — **ignore `Calculated_Rate__c` and `UnitPrice`** | Amounts equal |

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

Lead with a one-line verdict and quick links, then this exact table:

```
## Audit: <Opportunity Name> vs. <Contract Name>
**Links:** [Opportunity](https://postscript.lightning.force.com/lightning/r/Opportunity/<OPP_ID>/view) · [Account](https://postscript.lightning.force.com/lightning/r/Account/<ACCOUNT_ID>/view) · [SpotDraft Contract](<contract_link, e.g. https://app.spotdraft.com/contracts/v2/<numeric id>>)
**Owner:** <Opportunity Owner.Name>
**Result: X of Y checks passed — N mismatches, M warnings**

| # | Check | Contract | Salesforce | Result |
|---|---|---|---|---|
| 1 | Shop ID | 892108 | 892108 | ✅ Match |
| 2 | Contract Start Date | 2026-07-01 | 2026-06-01 | ❌ Mismatch |
| 7 | SMS Platform Fee | $2,000 (waived → $0) | $0 | ✅ Match |
| 5 | Opt-out date | — | — | ➖ N/A — not in contract |
...all 16 rows, in order...
```

When the audited contract is in signature stage (SIGN), insert
`**⚠ PRELIMINARY — contract not yet executed; terms may change before signing. Re-audit after execution.**`
directly under the Links line.

Result values: `✅ Match`, `❌ Mismatch`, `❌ Missing in SFDC`,
`⚠ <short note>` (judgment calls like shop-count multiples or near-miss),
`➖ N/A — not in contract`, `⛔ Blocked` (dependent check).

After the table, add a short **"What to fix"** list: one line per ❌, naming
the exact Salesforce field/line item to correct and the value it should be.
Show your work for any converted values (quarterly ÷ 3, waiver → $0). Do not
write to Salesforce or SpotDraft — this audit is read-only.

**Slack tagging — tag only on a call to action.** When posting the audit to
Slack, a **clean result (all applicable checks ✅, 0 mismatches and 0 warnings)
gets NO @-mention** — the table plus a ✅ reaction on the alert is the signal.
Only @-mention the owner when there is something actionable (a ❌ mismatch, a
⚠ warning, or "not yet auditable"). Routing when there *is* a finding: Renewal /
Upsell → Caitlin; New Business / Winback / Captured Account / Amendment → Lola;
+ Viv if the deal has Postscript Plus. In **#sfdc-oppty-audit** never tag Viv.

## Batch runs

When auditing multiple Opportunities, lead the report with a summary table
using exactly these columns — the Opportunity cell links to Salesforce and the
Contract cell links to SpotDraft:

```
| Opportunity | Owner | Contract | Passed | Mismatches |
|---|---|---|---|---|
| [Acme \| NB 1-2026](https://postscript.lightning.force.com/lightning/r/Opportunity/<OPP_ID>/view) | Jane Rep | [T-12345](https://app.spotdraft.com/contracts/v2/12345) | 14/16 | Start date; Min $1 s/b $500 |
```

Then include the full per-opp detail blocks below the table.
