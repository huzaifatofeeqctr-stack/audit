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
    return clients.soql(
        f"SELECT {LINE_FIELDS} FROM OpportunityLineItem WHERE OpportunityId = '{opp_id}'"
    )


def account_contracts(account_id):
    return clients.soql(
        "SELECT Name, Status__c, SpotDraft_ID__c, Date_Contract_Completed__c, CreatedDate "
        f"FROM SpotDraft_Contract__c WHERE Account__c = '{account_id}' ORDER BY CreatedDate DESC LIMIT 15"
    )


def pick_contract(contracts):
    """Newest Service-Order-type contract that is Completed or in Signing."""
    so = [c for c in contracts if "service order" in (c.get("Name") or "").lower()
          or "proposed" in (c.get("Name") or "").lower()]
    pool = so or contracts
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


def gather(opp, li_rows=None, contract_rows=None):
    """Build the data bundle. li_rows / contract_rows let a caller (n8n) inject
    pre-fetched Salesforce data so the worker doesn't query SF itself."""
    acct = (opp.get("Account") or {}).get("Name") or opp.get("AccountName")
    contracts = contract_rows if contract_rows is not None else account_contracts(opp["AccountId"])
    chosen = pick_contract(contracts)
    li = li_rows if li_rows is not None else line_items(opp["Id"])
    bundle = {
        "opportunity": {k: opp.get(k) for k in
                        ["Name", "StageName", "Type", "Shop_ID__c", "Start_Date__c",
                         "DocuSign_End_Date__c", "Opt_Out__c", "Opt_Out_Date__c",
                         "Minimum_Spend__c", "CloseDate", "Closed_Won_Reason__c"]},
        "owner": (opp.get("Owner") or {}).get("Name") or opp.get("OwnerName"),
        "account": acct,
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
def _routing_note(channel_id):
    no_viv = channel_id == SFDC
    return (
        "\n\n## Slack output instructions (apply on top of SKILL.md)\n"
        "Return ONLY the Slack message to post, as GitHub-flavored markdown, with a "
        "FIRST LINE of exactly `CLEAN: yes` or `CLEAN: no` (yes = every applicable check "
        "passed, 0 mismatches and 0 warnings; PRELIMINARY-but-otherwise-clean counts as yes). "
        "That first line will be stripped before posting.\n"
        "Tagging (only when CLEAN: no — clean audits get NO @-mention): "
        f"Renewal/Upsell -> <@{TAG['caitlin']}>; New Business/Winback/Captured Account/Amendment -> <@{TAG['lola']}>; "
        f"add <@{TAG['viv']}> only if the deal has Postscript Plus"
        + (" — but NEVER tag Viv in this channel." if no_viv else ".")
        + (" Never tag Viv here (this is #sfdc-oppty-audit)." if no_viv else "")
    )


def run_claude(channel_id, bundle):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    system = load_skill() + _routing_note(channel_id)
    user = (
        "Audit this Closed Won / Stage-5 opportunity against its SpotDraft contract, "
        "following the skill exactly. Data bundle (Salesforce opportunity, line items, "
        "SpotDraft contract list, and the chosen contract's key_pointers + extracted PDF "
        "text) follows as JSON:\n\n```json\n" + json.dumps(bundle, indent=1, default=str) + "\n```"
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def audit_message(alert_text, channel_id, sf=None):
    """Returns (slack_text, clean: bool) or raises.

    sf (optional): pre-fetched Salesforce data from n8n, shaped
      { "opp": <Opportunity SOQL record>, "lineItems": [...], "contracts": [...] }.
    When supplied, the worker uses it instead of querying Salesforce itself —
    this is how the n8n flow feeds SF data and sidesteps the worker's SF creds.
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
    else:
        opp = find_opportunity(name)
        if not opp:
            return (f"Could not find an auditable Opportunity named *{name}* "
                    "(Closed Won / Stage 5 / Pricing & Negotiations).", False)
    bundle = gather(opp, li_rows, contract_rows)
    out = run_claude(channel_id, bundle)
    clean = False
    m = re.match(r"\s*CLEAN:\s*(yes|no)\s*\n", out, re.I)
    if m:
        clean = m.group(1).lower() == "yes"
        out = out[m.end():].lstrip()
    return out, clean
