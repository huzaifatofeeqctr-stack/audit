---
name: closed-won-contract-audit
description: >-
  Audit a Salesforce Closed Won Opportunity against its executed SpotDraft
  Service Order contract to catch data-entry mismatches before they hit
  billing. Use this skill whenever Caitlin (or anyone on Postscript billing/
  deal desk/RevOps) asks to audit, reconcile, QA, double-check, or verify a
  Closed Won Opportunity against its contract or Service Order — including
  phrases like "audit this opp against the contract", "check the closed won
  opp vs SpotDraft", "verify the service order matches Salesforce", "run the
  closed won audit", "did this deal get entered correctly", or when they share
  a SpotDraft contract (link or PDF) together with an Opportunity name/link
  and any review intent. Trigger even if they don't say "audit" — any request
  to compare contracted Service Order terms (dates, fees, package, minimum
  commitment, DSC, Plus, AI addendums, signer, opt-out) to what was entered on
  the Opportunity and its Product Line Items is this skill.
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

**Gate checks before auditing** (report and stop if either fails):
- Opportunity `StageName` = "Closed Won"
- Contract status = Executed (signed by both parties)

## Reading the contract

A Postscript Service Order has a main **Postscript Service Order section**
plus optional addendums: **SMS Marketing Addendum**, **Postscript Plus
Addendum**, **Postscript AI Addendum**. Pull these values:

| Contract field | Where it lives |
|---|---|
| Shop ID(s) | "Shop(s)" row — the number in parentheses, e.g. `LOOK OPTIC (892108)` |
| Customer signer name + title | Customer signature block (not the Postscript signer) |
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

Three queries cover everything. Use the real Opportunity Id.

```sql
SELECT Id, Name, Type, StageName, CloseDate, AccountId, Account.Name,
       Shop_ID__c, Start_Date__c, DocuSign_End_Date__c, Opt_Out__c,
       Opt_Out_Date__c, Minimum_Spend__c
FROM Opportunity WHERE Id = '<OPP_ID>'
```

```sql
SELECT Id, Product2.Name, Package_Type__c, Platform_Fee__c,
       Monthly_Fee_Minimum__c, Number_Of_Months__c, UnitPrice
FROM OpportunityLineItem WHERE OpportunityId = '<OPP_ID>'
```

```sql
SELECT Contact.Name, Contact.Title, Contact.Email, Role, IsPrimary
FROM OpportunityContactRole WHERE OpportunityId = '<OPP_ID>'
```

Field gotchas learned from the live org — trust these over labels:
- **Dates**: use `Start_Date__c` (label "Contract Start Date") and
  `DocuSign_End_Date__c` (label "Contract End Date"). The fields named
  `Contract_Start_Date__c` / `Contract_End_Date__c` are deprecated — never
  use them.
- **Line item "Monthly Fee"** is API name `Platform_Fee__c` (label/API
  mismatch).
- **Ignore `Calculated_Rate__c`** on line items entirely — it is explicitly
  out of scope for fee checks. Also do not use `UnitPrice` for fee checks;
  the contracted monthly fee lives in `Platform_Fee__c`.
- **Do not compare line-item `Start_Date__c`/`End_Date__c` to contract
  dates** — those get auto-stamped from the opp close date and are not what
  reps maintain. The rep-entered service-length field is
  `Number_Of_Months__c` (the "Number of Months" input on the Add/Edit
  Product form); derive the service window from it (see check 14).
- Line items are identified by `Product2.Name`: "SMS Marketing",
  "Dedicated Short Code", "Postscript Plus", "Postscript AI" (plus others
  like "Shopper", "IT Campaigns", "IT Automations" that this audit does not
  check).

## The checks

Run all of these. A blank/null Salesforce value where the contract has a
value is a **mismatch** (report as "missing in SFDC"), not a skip.

| # | Check | Contract value | Salesforce value | Pass when |
|---|---|---|---|---|
| 1 | Shop ID | Shop ID(s) from "Shop(s)" row | Opportunity `Shop_ID__c` | Every contract Shop ID appears (exact string match per ID). **New Business exception:** if the contract has no usable Shop ID (name-only row or a "(0)" placeholder) and the Opportunity `Type` is "New Business", the shop usually didn't exist yet at signing — mark ➖ N/A, don't flag. For any other Type, a missing/unverifiable Shop ID is still a ⚠ |
| 2 | Signer is a Related Contact | Customer signer name | `OpportunityContactRole` Contact.Name list | Signer present in **any** role (primary not required) |
| 3 | Signer title | Signer title from signature block | That contact's `Contact.Title` | Titles match (case-insensitive; minor punctuation differences OK, different titles are a fail) |
| 4 | Contract Start Date | Service Order Term Start Date | Opportunity `Start_Date__c` | Exact date match |
| 5 | Contract End Date | Service Order Term End Date | Opportunity `DocuSign_End_Date__c` | Exact date match |
| 6 | Opt-out flag | Service Order-level termination-for-convenience clause exists (any heading)? | Opportunity `Opt_Out__c` | Language present → `true`; absent → `false` |
| 7 | Opt-out date | Effective date of the termination right | Opportunity `Opt_Out_Date__c` | Exact date match (N/A if no opt-out language and flag is correctly false) |
| 8 | Package type | SMS Addendum Package | SMS Marketing line item `Package_Type__c` | Exact picklist match |
| 9 | SMS Platform Fee | SMS Addendum Platform Fee, **waiver-adjusted** | SMS Marketing line item `Platform_Fee__c` | Amounts equal. **Waiver rule: if the contract waives the fee, the expected SF value is $0**, not the stated fee. **For this check only, a blank/null `Platform_Fee__c` counts as $0** — so blank = ✅ Match when the contract fee is waived or $0 |
| 10 | Minimum Commitment | Minimum Commitment **converted to monthly** (quarterly amount ÷ 3) | Opportunity `Minimum_Spend__c` | Amounts equal (this opp field mirrors the line item, so one check suffices) |
| 11 | DSC included | SMS Addendum has a Dedicated Short Code section with DSC fee? | "Dedicated Short Code" line item exists | Both present or both absent |
| 12 | DSC monthly fee | DSC fee, **waiver-adjusted** (waived → $0) | DSC line item `Platform_Fee__c` | Amounts equal. **A blank/null `Platform_Fee__c` counts as $0** — blank = ✅ Match when the contract fee is waived or $0 (a blank against a real discounted fee like $200 is still a mismatch) |
| 13 | Plus included | Postscript Plus Addendum present? | "Postscript Plus" line item exists | Both present or both absent |
| 14 | Plus service dates | Plus Addendum Start/End Dates | Window derived from Plus line item `Number_Of_Months__c`: start = Service Order Start Date, end = start + N months − 1 day (e.g. start 2026-06-01, N=4 → 2026-06-01 to 2026-09-30) | Derived window equals the Plus Addendum Start/End Dates. **Mid-month tolerance:** contracts with mid-month terms (e.g. 6/15 → 10/15) span N months + 1 day, which no integer N can reproduce — if the derived end is within 1 day of the contracted end, count it as ✅ Match with a parenthetical note. A gap larger than 1 day (e.g. derived 11/14 vs contracted 11/30) is a real mismatch. |
| 15 | Plus package | Plus Addendum package tier (e.g. "Plus Launch" → "Launch") | Plus line item `Package_Type__c` | Tier matches |
| 16 | Plus monthly fee | Plus Addendum Fees ($/mo per Shop) | Plus line item `Platform_Fee__c` | Amounts equal |
| 17 | AI included | Postscript AI Addendum present? | "Postscript AI" line item exists | Both present or both absent |
| 18 | AI Platform Fee | AI Addendum "AI Platform Fee Price" ($/mo per Shop) | AI line item `Platform_Fee__c` — **ignore `Calculated_Rate__c` and `UnitPrice`** | Amounts equal |

Conditional checks (7, 12, 14–16, 18) become **N/A — not in contract** when
the underlying addendum/section is absent, as long as the corresponding
"included" check passed. If an addendum is missing where the contract has
one (or vice versa), report the inclusion mismatch and mark the dependent
checks "blocked".

Multi-shop contracts: per-Shop fees ($/mo per Shop) are expected to be
multiplied by the number of shops only if the line item represents all shops —
if amounts differ by an exact shop-count multiple, flag it as a ⚠ with a note
rather than a hard fail, and say why.

## Output format

Lead with a one-line verdict and quick links, then this exact table:

```
## Audit: <Opportunity Name> vs. <Contract Name>
**Links:** [Opportunity](https://postscript.lightning.force.com/lightning/r/Opportunity/<OPP_ID>/view) · [Account](https://postscript.lightning.force.com/lightning/r/Account/<ACCOUNT_ID>/view) · [SpotDraft Contract](<contract_link from get_contract_list, e.g. https://app.spotdraft.com/contracts/v2/<numeric id>>)
**Result: X of Y checks passed — N mismatches, M warnings**

| # | Check | Contract | Salesforce | Result |
|---|---|---|---|---|
| 1 | Shop ID | 892108 | 892108 | ✅ Match |
| 4 | Contract Start Date | 2026-07-01 | 2026-06-01 | ❌ Mismatch |
| 9 | SMS Platform Fee | $2,000 (waived → $0) | $0 | ✅ Match |
| 7 | Opt-out date | — | — | ➖ N/A — not in contract |
...all 18 rows, in order...
```

Result values: `✅ Match`, `❌ Mismatch`, `❌ Missing in SFDC`,
`⚠ <short note>` (judgment calls like shop-count multiples or near-miss
titles), `➖ N/A — not in contract`, `⛔ Blocked` (dependent check).

After the table, add a short **"What to fix"** list: one line per ❌, naming
the exact Salesforce field/line item to correct and the value it should be.
Show your work for any converted values (quarterly ÷ 3, waiver → $0) so the
reviewer can verify the math. Do not write to Salesforce or SpotDraft —
this audit is read-only.
