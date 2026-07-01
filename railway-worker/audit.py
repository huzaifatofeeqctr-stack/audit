"""Audit pipeline: gather Salesforce + SpotDraft data, let Claude run the
16-check audit using SKILL.md as the system prompt, and return a Slack-ready
message plus a clean/finding flag.

The division of labor mirrors the PRD: this code *gathers* data; SKILL.md (the
Claude system prompt) is the *brain* that applies the audit rules, contract
selection, auto-renewal handling, and tagging.
"""
import os
import re
import time
import json
import requests
import anthropic

import clients

MODEL = os.environ.get("AUDIT_MODEL", "claude-opus-4-8")
SKILL_SOURCE = os.environ.get(
    "SKILL_SOURCE",
    # raw GitHub URL so SKILL.md edits ship via git with no redeploy
    "https://raw.githubusercontent.com/huzaifatofeeqctr-stack/audit/claude/kind-tesla-9l7gw/SKILL.md",
)

CLOSED_WON = "C08JS7N86D6"   # #closed-won-presales
SFDC = "C0B88KMFJ3E"          # #sfdc-oppty-audit
TAG = {"caitlin": "U077EJVK10R", "lola": "U07GQE3BP7F", "viv": "U08CPAGU1DZ"}

_skill_cache = {"text": None, "exp": 0}


def load_skill():
    if _skill_cache["text"] and time.time() < _skill_cache["exp"]:
        return _skill_cache["text"]
    text = None
    try:
        r = requests.get(SKILL_SOURCE, timeout=20)
        if r.ok:
            text = r.text
    except Exception:
        pass
    if not text:
        for p in ("SKILL.md", "../SKILL.md"):
            if os.path.exists(p):
                text = open(p, encoding="utf-8").read()
                break
    if not text:
        raise RuntimeError("Could not load SKILL.md from SKILL_SOURCE or local file")
    _skill_cache.update(text=text, exp=time.time() + 300)
    return text


def parse_opp_name(alert_text: str):
    m = re.search(r"\*Name:\*\s*(.+)", alert_text)
    return m.group(1).strip() if m else None


def shop_id_from_key_pointers(key_pointers):
    """Pull the shop ID from a contract's SpotDraft key_pointers. The ShopNameID
    field holds e.g. 'Glow Recipe (25819)' — return '25819'. Returns None if
    the field is absent/unparseable. (SpotDraft_Contract__c has no shop-id
    field, so the contract's shop id only lives here.)"""
    if not isinstance(key_pointers, list):
        return None
    for kp in key_pointers:
        if not isinstance(kp, dict):
            continue
        if kp.get("field_name") == "slug_shopnameid" or kp.get("label") == "ShopNameID":
            v = kp.get("value")
            if isinstance(v, str):
                m = re.search(r"\((\d+)\)", v) or re.search(r"\b(\d{3,})\b", v)
                if m:
                    return m.group(1)
    return None


# ----------------------------------------------------------- data gathering
OPP_FIELDS = (
    "Id, Name, StageName, Type, AccountId, Account.Name, Owner.Name, Shop_ID__c, "
    "Start_Date__c, DocuSign_End_Date__c, Opt_Out__c, Opt_Out_Date__c, "
    "Minimum_Spend__c, CloseDate, Closed_Won_Reason__c"
)
LINE_FIELDS = "Product2.Name, Package_Type__c, Platform_Fee__c, Number_Of_Months__c, UnitPrice"


def _esc(v):
    return v.replace("'", r"\'")


def find_opportunity(name):
    rows = clients.soql(
        f"SELECT {OPP_FIELDS} FROM Opportunity WHERE Name = '{_esc(name)}' "
        "AND (StageName = 'Closed Won' OR StageName LIKE '5%' OR StageName = 'Pricing & Negotiations') "
        "ORDER BY CreatedDate DESC LIMIT 1"
    )
    return rows[0] if rows else None


def line_items(opp_id):
    # Fetch via the parent Opportunity's child relationship rather than
    # `FROM OpportunityLineItem` directly: OpportunityLineItem has no standalone
    # object permission (access derives from Opportunity + Price Book), so a
    # restricted integration user can hit INVALID_TYPE on the direct query but
    # still read the children through the parent it already has access to.
    rows = clients.soql(
        f"SELECT Id, (SELECT {LINE_FIELDS} FROM OpportunityLineItems) "
        f"FROM Opportunity WHERE Id = '{opp_id}'"
    )
    if not rows:
        return []
    child = rows[0].get("OpportunityLineItems") or {}
    return child.get("records", [])


def account_contracts(account_id):
    return clients.soql(
        "SELECT Name, Status__c, SpotDraft_ID__c, Date_Contract_Completed__c, CreatedDate "
        f"FROM SpotDraft_Contract__c WHERE Account__c = '{account_id}' ORDER BY CreatedDate DESC LIMIT 15"
    )


_GOVERNING_DOC = ("service order", "proposed", "statement of work", "sow",
                  "contract addendum", "amendment", "order form")


def pick_contract(contracts):
    """Newest governing contract that is Completed or in Signing. `contracts` is
    ordered newest-first, so the first match is the most recent governing doc.

    Governing docs include Service Orders AND Statements of Work / Contract
    Addendums — an Upsell/Amendment is papered by a **SOW attached to the upsell**,
    not the base Service Order. Selecting only 'Service Order'-named contracts made
    the agent audit an amendment against the prior base SO (wrong dates/terms)."""
    def is_governing(c):
        n = (c.get("Name") or "").lower()
        return any(k in n for k in _GOVERNING_DOC)
    pool = [c for c in contracts if is_governing(c)] or contracts
    for status in ("Completed", "Signing"):
        for c in pool:
            if c.get("Status__c") == status and c.get("SpotDraft_ID__c"):
                return c
    return None


def _li_product(li):
    """Product name from a SOQL row (nested Product2.Name) or a flat 'Product'."""
    p2 = li.get("Product2")
    if isinstance(p2, dict):
        return p2.get("Name")
    return li.get("Product") or li.get("Product2.Name")


def gather(opp, li_rows=None, contract_rows=None, force_tid=None):
    """Build the data bundle. li_rows / contract_rows let a caller (n8n) inject
    pre-fetched Salesforce data so the worker doesn't query SF itself.
    force_tid pins the chosen contract to a specific SpotDraft T-id (used by the
    signature-notifier path, where we must audit the SO that just went to
    Signing — not whatever pick_contract's newest-Completed heuristic returns)."""
    acct = (opp.get("Account") or {}).get("Name") or opp.get("AccountName")
    contracts = contract_rows if contract_rows is not None else account_contracts(opp["AccountId"])
    if force_tid:
        chosen = next((c for c in contracts if c.get("SpotDraft_ID__c") == force_tid), None) \
            or {"SpotDraft_ID__c": force_tid, "Status__c": "Signing"}
    else:
        chosen = pick_contract(contracts)
    li = li_rows if li_rows is not None else line_items(opp["Id"])
    bundle = {
        "opportunity": {k: opp.get(k) for k in
                        ["Name", "StageName", "Type", "Shop_ID__c", "Start_Date__c",
                         "DocuSign_End_Date__c", "Opt_Out__c", "Opt_Out_Date__c",
                         "Minimum_Spend__c", "CloseDate", "Closed_Won_Reason__c"]},
        "owner": (opp.get("Owner") or {}).get("Name") or opp.get("OwnerName"),
        "account": acct,
        "opp_id": opp.get("Id"),
        "account_id": opp.get("AccountId"),
        "line_items": [{k: row.get(k) for k in ["Package_Type__c", "Platform_Fee__c",
                        "Number_Of_Months__c", "UnitPrice"]} | {"Product": _li_product(row)}
                       for row in li],
        "spotdraft_contracts": [{k: c.get(k) for k in
                                 ["Name", "Status__c", "SpotDraft_ID__c", "Date_Contract_Completed__c"]}
                                for c in contracts],
        "chosen_contract": None,
    }
    if chosen:
        tid = chosen["SpotDraft_ID__c"]
        bundle["chosen_contract"] = {"T_id": tid, "status": chosen["Status__c"]}
        try:
            bundle["chosen_contract"]["key_pointers"] = clients.spotdraft_key_pointers(tid)
        except Exception as e:
            bundle["chosen_contract"]["key_pointers_error"] = str(e)
        try:
            bundle["chosen_contract"]["pdf_text"] = clients.spotdraft_pdf_text(tid)
        except Exception as e:
            bundle["chosen_contract"]["pdf_error"] = str(e)
    return bundle


# ------------------------------------------------------------------- Claude
AUDIT_TOOL = {
    "name": "submit_audit",
    "description": (
        "Return the completed 16-check audit as structured data. The worker "
        "renders the Slack message and computes the verdict from this — output "
        "data only, never prose, and never a second/revised submission."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "preliminary": {
                "type": "boolean",
                "description": "true iff the audited contract is in a signature stage "
                               "(SIGN / Signing / Awaiting Signature), not yet executed.",
            },
            "contract_label": {
                "type": "string",
                "description": "How to name the contract, e.g. "
                               "'Postscript Service Order (T-460362)'.",
            },
            "checks": {
                "type": "array",
                "description": "All 16 checks, in order 1..16. Include every check.",
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "name": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["match", "mismatch", "missing_sfdc",
                                     "warning", "na", "blocked"],
                        },
                        "fix": {
                            "type": "string",
                            "description": "Set ONLY when the value genuinely differs (status "
                                           "mismatch / missing_sfdc / actionable warning): a terse "
                                           "one-line correction naming the exact Salesforce field "
                                           "or line item and its corrected value, e.g. 'Postscript "
                                           "AI `Platform_Fee__c`: $599 → $199'. The two sides MUST "
                                           "differ — never a no-op like '$699 → $699' or "
                                           "'value correct'/'no change' (that is a `match`, leave "
                                           "empty). Show a converted value only when needed "
                                           "(qtr ÷ 3, waiver → $0). No explanatory sentences. "
                                           "**When a line item is MISSING in SFDC, state the fee/"
                                           "value to enter from the contract, e.g. 'Add Postscript "
                                           "AI line item — `Platform_Fee__c` $99' or 'Add Dedicated "
                                           "Short Code line item — $250'** (never just 'add X line "
                                           "item' with no value). Empty for match / na.",
                        },
                    },
                    "required": ["n", "name", "status"],
                },
            },
            "note": {
                "type": "string",
                "description": "At most ONE short sentence of essential context (e.g. a waiver "
                               "window, or a multi-shop per-shop allocation). Usually empty.",
            },
        },
        "required": ["preliminary", "checks"],
    },
}

_AUDIT_INSTRUCTIONS = (
    "\n\n## How to return the audit\n"
    "Evaluate ALL 16 checks against the data bundle, then call the `submit_audit` tool "
    "ONCE with the result. Do not write any prose, reasoning, or a Slack message — the "
    "worker renders everything from your structured submission.\n"
    "- Populate every one of the 16 checks with its status. Never drop a check.\n"
    "- For EACH check whose status is `mismatch`, `missing_sfdc`, or an actionable `warning`, "
    "supply a terse `fix` (the worker turns these into the 'Updates Required' bullets). Every "
    "discrepancy on the deal must have its own fix — never report only the first/largest.\n"
    "- NEVER flag a value that matches. A check is `mismatch`/`warning` ONLY when the contract "
    "and Salesforce values genuinely differ (money: by more than $5). If they agree, status is "
    "`match` with NO fix. Never emit a no-op fix like `$1,250 → $1,250`, `$699 → $699`, "
    "'value correct', 'no change', 'matches', or 'OK' — that is a `match`. Zero real differences "
    "=> the deal is clean.\n"
    "- Plus package (check 13): the SFDC `Package_Type__c` is the bare tier — `Essentials`, "
    "`Signature`, or `Launch`, with NO 'Plus' prefix. Contract 'Plus Signature' vs SFDC "
    "'Signature' is a MATCH. Never suggest changing it to 'Plus Signature'/'Plus Launch'/etc.\n"
    "- Do not hallucinate Plus inclusion: only mark Plus included (11) when there is a real "
    "Postscript Plus Addendum SECTION in the PDF AND a 'Postscript Plus' line item.\n"
    "- `preliminary` is true iff the chosen contract is in a signature stage (SIGN).\n"
    "## Evidence rule (critical)\n"
    "Product inclusion (DSC check 9, Plus check 11, AI check 15) is determined ONLY by "
    "(a) an actual addendum SECTION present in the contract PDF text AND (b) the matching SFDC "
    "line item. The `key_pointers` often contain unpopulated PLACEHOLDER/template fields — e.g. "
    "`AIPlatformFeePrice` ($699), `AIShops`, `Shopper`, `Infinity Testing` — that are NOT evidence "
    "a product was sold. IGNORE them. If the PDF has no such addendum and there is no SFDC line "
    "item, the check is `match` (both correctly absent), not a warning."
)


def run_audit(bundle):
    """Run the audit and return the model's structured result (dict) via a forced
    tool call. Structured output (vs free-form text) makes the verdict and the
    Slack rendering deterministic — the model can't leak self-correction prose,
    emit a duplicate table, or undercount a mismatch."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = load_skill() + _AUDIT_INSTRUCTIONS
    user = (
        "Audit this Closed Won / Stage-5 opportunity against its SpotDraft contract, "
        "following the skill exactly, then call submit_audit. Data bundle (Salesforce "
        "opportunity, line items, SpotDraft contract list, and the chosen contract's "
        "key_pointers + extracted PDF text) follows as JSON:\n\n```json\n"
        + json.dumps(bundle, indent=1, default=str) + "\n```"
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        system=system,
        tools=[AUDIT_TOOL],
        tool_choice={"type": "tool", "name": "submit_audit"},
        messages=[{"role": "user", "content": user}],
    )
    for b in msg.content:
        if b.type == "tool_use" and b.name == "submit_audit":
            return b.input
    raise RuntimeError("model did not return a submit_audit tool call")


def _spotdraft_url(tid):
    m = re.search(r"(\d+)", tid or "")
    return f"https://app.spotdraft.com/contracts/v2/{m.group(1)}" if m else None


_MISMATCH = ("mismatch", "missing_sfdc")
_ACTIONABLE = ("mismatch", "missing_sfdc", "warning")
_NOOP_PHRASES = ("value correct", "no change", "no update", "matches", "unchanged",
                 "already correct", "correct —", "correct-", "— ok", "(ok)", "s/b same",
                 "no fix", "is correct", "no changes")


def _value_token(s, last=False):
    """Pull the comparable value from one side of a `→`: prefer a date, then a
    money/number, else the first/last word. `last=True` takes the token nearest
    the arrow on the left side; otherwise the first token on the right side
    (skipping trailing notes like 'per SO AI Addendum')."""
    nums = re.findall(r"\d{4}-\d{2}-\d{2}|[\d,]+(?:\.\d+)?", s)
    if nums:
        return (nums[-1] if last else nums[0]).replace(",", "")
    words = re.findall(r"[A-Za-z][\w]*", s)
    if words:
        return (words[-1] if last else words[0]).lower()
    return ""


def _is_noop_fix(fix):
    """True if a 'fix' says nothing needs changing — e.g. '$699 → $699 per SO AI
    Addendum', '$1,250 → $1,250 — value correct', 'matches'. These must never
    become an Updates Required bullet or count as a mismatch (Caitlin: a matching
    value should read clean)."""
    if not fix:
        return True
    if any(p in fix.lower() for p in _NOOP_PHRASES):
        return True
    if "→" not in fix:
        return False
    left, right = fix.split("→", 1)
    if ":" in left:                      # drop the field-name label
        left = left.rsplit(":", 1)[1]
    lv = _value_token(left, last=True)   # value just before the arrow
    rv = _value_token(right, last=False)  # value just after the arrow
    return bool(lv) and lv == rv


def _render_slack(result, bundle):
    """Build the concise Slack message from the model's structured audit and
    compute the verdict deterministically. Returns (slack_text, clean).

    Format (the one Sales Ops asked for): header + links + owner + a one-line
    Result, then an 'Updates Required' bullet per fix — NO 16-row table.
    No-op 'fixes' (value already matches) are dropped and do NOT count as issues."""
    checks = [c for c in (result.get("checks") or []) if isinstance(c, dict)]
    # genuine, actionable fixes only (drop matching-value false flags)
    real = [c for c in checks if c.get("status") in _ACTIONABLE
            and not _is_noop_fix((c.get("fix") or "").strip())]
    nmis = sum(1 for c in real if c.get("status") in _MISMATCH)
    nwarn = sum(1 for c in real if c.get("status") == "warning")
    nblk = sum(1 for c in checks if c.get("status") == "blocked")
    clean = nmis == 0 and nwarn == 0 and nblk == 0
    passed = 16 - nmis - nwarn - nblk

    opp = bundle.get("opportunity") or {}
    name = opp.get("Name") or "(unknown opportunity)"
    tid = (bundle.get("chosen_contract") or {}).get("T_id")
    label = result.get("contract_label") or (f"Postscript Service Order ({tid})" if tid else "the Service Order")

    lines = [f"*Audit: {name} vs. {label}*"]
    links = []
    if bundle.get("opp_id"):
        links.append(f"<https://postscript.lightning.force.com/lightning/r/Opportunity/{bundle['opp_id']}/view|Opportunity>")
    if bundle.get("account_id"):
        links.append(f"<https://postscript.lightning.force.com/lightning/r/Account/{bundle['account_id']}/view|Account>")
    sd = _spotdraft_url(tid)
    if sd:
        links.append(f"<{sd}|SpotDraft Contract>")
    if links:
        lines.append("*Links:* " + " · ".join(links))
    if bundle.get("owner"):
        lines.append(f"*Owner:* {bundle['owner']}")
    if result.get("preliminary"):
        lines.append("*:warning: PRELIMINARY — contract in signature stage, not yet executed; "
                     "terms may change before signing. Re-audit after execution.*")

    if clean:
        lines.append("*Result: 16 of 16 checks passed — clean* :white_check_mark:")
        lines.append("")
        lines.append(":white_check_mark: Clean — nothing to fix.")
    else:
        parts = []
        if nmis:
            parts.append(f"{nmis} mismatch{'' if nmis == 1 else 'es'}")
        if nwarn:
            parts.append(f"{nwarn} warning{'' if nwarn == 1 else 's'}")
        if nblk:
            parts.append(f"{nblk} blocked")
        lines.append(f"*Result: {passed} of 16 checks passed — {', '.join(parts)}*")
        lines.append("")
        lines.append("*Updates Required:*")
        for c in real:
            fix = (c.get("fix") or "").strip()
            lines.append(f"• {fix or c.get('name', 'see contract')}")

    note = (result.get("note") or "").strip()
    if note:
        lines.append("")
        lines.append(f"_{note}_")
    return "\n".join(lines), clean


def audit_message(alert_text, channel_id, sf=None, contract_tid=None, opp_record=None):
    """Returns (slack_text, clean: bool) or raises.

    sf (optional): pre-fetched Salesforce data from n8n, shaped
      { "opp": <Opportunity SOQL record>, "lineItems": [...], "contracts": [...] }.
    When supplied, the worker uses it instead of querying Salesforce itself —
    this is how the n8n flow feeds SF data and sidesteps the worker's SF creds.

    opp_record (optional): a pre-resolved Opportunity SOQL record to audit
    directly (used by the signature path, which resolves an open/pre-close opp
    via Shop ID — find_opportunity's closed-stage filter would reject it). When
    set, gather() still fetches line items + contracts itself.
    """
    name = parse_opp_name(alert_text)
    if not name:
        raise ValueError("no '*Name:*' line in alert")
    li_rows = contract_rows = None
    if sf is not None:
        # n8n is driving — use only the injected data, never fall back to SF
        opp = sf.get("opp")
        if isinstance(opp, list):
            opp = opp[0] if opp else None
        if not opp:
            return (f"Could not find an auditable Opportunity named *{name}* "
                    "(Closed Won / Stage 5 / Pricing & Negotiations).", False)
        li_rows = sf.get("lineItems", sf.get("line_items")) or []
        contract_rows = sf.get("contracts") or []
    elif opp_record is not None:
        # caller already resolved the opp (e.g. signature path, open-stage opp)
        opp = opp_record
    else:
        opp = find_opportunity(name)
        if not opp:
            return (f"Could not find an auditable Opportunity named *{name}* "
                    "(Closed Won / Stage 5 / Pricing & Negotiations).", False)
    bundle = gather(opp, li_rows, contract_rows, force_tid=contract_tid)
    result = run_audit(bundle)
    out, clean = _render_slack(result, bundle)

    # Deterministic tagging — clean => no @-mention; otherwise route by opp Type
    # (Renewal / Upsell / Existing Business -> Caitlin; else Lola) + Viv if the
    # deal has Postscript Plus, but never Viv in #sfdc-oppty-audit.
    if not clean:
        typ = (opp.get("Type") or "").lower()
        who = [TAG["caitlin"] if typ in ("renewal", "upsell", "existing business") else TAG["lola"]]
        has_plus = any(li.get("Product") == "Postscript Plus" for li in bundle.get("line_items", []))
        if has_plus and channel_id != SFDC:
            who.append(TAG["viv"])
        out = out.rstrip() + "\n\ncc " + " ".join(f"<@{w}>" for w in who)
    return out, clean
