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
def _routing_note(channel_id):
    no_viv = channel_id == SFDC
    return (
        "\n\n## Slack output instructions (apply on top of SKILL.md)\n"
        "Return ONLY the Slack message to post, as GitHub-flavored markdown, with a "
        "FIRST LINE of exactly `CLEAN: yes` or `CLEAN: no` (yes = every applicable check "
        "passed, 0 mismatches and 0 warnings; PRELIMINARY-but-otherwise-clean counts as yes). "
        "That first line will be stripped before posting.\n"
        "Use the real IDs from the data bundle for links: the Opportunity link is "
        "`https://postscript.lightning.force.com/lightning/r/Opportunity/<opp_id>/view` and the "
        "Account link uses `<account_id>` — never leave `<OPP_ID>`/`<ACCOUNT_ID>` placeholders.\n"
        "Do NOT add an auto-renewal preamble/banner unless this is a TRUE auto-renewal "
        "(no new SO; audited against a prior-term SO). For freshly signed deals, omit it entirely.\n"
        "Tagging (only when CLEAN: no — clean audits get NO @-mention): "
        f"Renewal/Upsell -> <@{TAG['caitlin']}>; New Business/Winback/Captured Account/Amendment -> <@{TAG['lola']}>; "
        f"add <@{TAG['viv']}> only if the deal has Postscript Plus"
        + (" — but NEVER tag Viv in this channel." if no_viv else ".")
        + (" Never tag Viv here (this is #sfdc-oppty-audit)." if no_viv else "")
        + "\n\n## Evidence rule (critical)\n"
        "Product inclusion (DSC check 9, Plus check 11, AI check 15) is determined ONLY by "
        "(a) an actual addendum SECTION present in the contract PDF text, AND (b) the matching "
        "SFDC line item. The contract `key_pointers` often contain unpopulated PLACEHOLDER/template "
        "fields — e.g. `AIPlatformFeePrice` ($699), `AIShops`, `Shopper`, `Infinity Testing` — that "
        "are NOT evidence a product was sold. IGNORE them. Never raise a ⚠ or ❌ merely because a "
        "key-pointer is populated. If the PDF has no such addendum and there is no SFDC line item, "
        "the check is ✅ (both correctly absent), not a warning."
        + "\n\n## Output discipline (critical)\n"
        "- Output the FINAL message only. NO reasoning, deliberation, self-correction, or "
        "meta-commentary. Never write words like 'Wait', 'rechecking', 'correcting', 'actually', "
        "and never emit a second/duplicate Result line. Decide the verdict before writing, write it once.\n"
        "- The very first line MUST be exactly `CLEAN: yes` or `CLEAN: no` and appear nowhere else.\n"
        "- The `Result: X of 16 checks passed — N mismatches, M warnings` counts MUST match the table "
        "exactly: X = count of ✅ rows, N = count of ❌ rows, M = count of ⚠ rows. Count the rows, then write the line.\n"
        "- If you catch yourself wanting to revise, regenerate silently — never show the revision."
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


def _verdict_from_table(out):
    """Count ❌/⚠/⛔ in the audit TABLE only (not banners/notes), and decide
    clean deterministically. The model's own header counts and CLEAN line are
    unreliable, so we derive the verdict from the table it produced.

    Returns (clean: bool, has_table: bool, mismatches, warnings, blocked).
    """
    rows = [l for l in out.splitlines() if l.lstrip().startswith("|")]
    has_table = len(rows) >= 10  # a real 16-check table; else it's a not-auditable/error note
    body = "\n".join(rows)
    nmis = body.count("❌")
    nwarn = body.count("⚠")
    nblk = body.count("⛔")
    clean = has_table and nmis == 0 and nwarn == 0 and nblk == 0
    return clean, has_table, nmis, nwarn, nblk


def _to_slack(md):
    """Convert the model's GitHub-Markdown into Slack mrkdwn that actually
    renders: headings/bold -> *bold*, [t](u) -> <u|t>, bullets -> •, and the
    pipe table -> an aligned monospace code block (Slack has no Markdown tables)."""
    out, tbl = [], []

    def flush_table():
        if not tbl:
            return
        rows = []
        for r in tbl:
            cells = [c.strip() for c in r.strip().strip("|").split("|")]
            # drop the |---|---| separator row
            if cells and all(c and set(c) <= set("-: ") for c in cells):
                continue
            rows.append(cells)
        tbl.clear()
        if not rows:
            return
        ncol = max(len(r) for r in rows)
        rows = [r + [""] * (ncol - len(r)) for r in rows]
        w = [max(len(r[c]) for r in rows) for c in range(ncol)]
        body = []
        for r in rows:
            # pad every column except the last (last holds emoji of varying width)
            cells = [r[c].ljust(w[c]) for c in range(ncol - 1)] + [r[ncol - 1]]
            body.append("  ".join(cells).rstrip())
        out.append("```\n" + "\n".join(body) + "\n```")

    for ln in md.split("\n"):
        if ln.lstrip().startswith("|"):
            tbl.append(ln)
            continue
        flush_table()
        s = re.sub(r"^\s*#{1,6}\s*(.*)$", r"*\1*", ln)          # headings -> bold
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", s)  # links
        s = s.replace("**", "*")                                  # bold
        s = re.sub(r"^\s*[-*]\s+", "• ", s)                       # bullets
        out.append(s)
    flush_table()
    return "\n".join(out).strip()


def audit_message(alert_text, channel_id, sf=None, contract_tid=None):
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
    bundle = gather(opp, li_rows, contract_rows, force_tid=contract_tid)
    out = run_claude(channel_id, bundle)
    # Strip any CLEAN: line the model emitted — we derive the verdict ourselves.
    out = re.sub(r"(?im)^\s*CLEAN:\s*(?:yes|no)\s*$", "", out).strip()
    # Deterministic verdict from the table the model produced (its own header
    # counts / CLEAN line were unreliable).
    clean, has_table, nmis, nwarn, nblk = _verdict_from_table(out)
    if has_table:
        passed = 16 - nmis - nwarn - nblk
        mw = f"{nmis} mismatch{'' if nmis == 1 else 'es'}, {nwarn} warning{'' if nwarn == 1 else 's'}"
        if nblk:
            mw += f", {nblk} blocked"
        # rewrite the Result line so the header always matches the table
        out = re.sub(r"(?im)^\**\s*Result:.*$",
                     f"**Result: {passed} of 16 checks passed — {mw}**", out, count=1)

    # Deterministic tagging — never trust the model here. Strip any @-mentions /
    # cc lines it emitted, then: clean => no tag; otherwise route by opp Type
    # (Renewal/Upsell -> Caitlin; else Lola) + Viv if Plus, never Viv in #sfdc.
    out = re.sub(r"(?im)^\s*cc\b.*$", "", out)
    out = re.sub(r"<@U[A-Z0-9]+>", "", out).rstrip()
    if not clean:
        typ = (opp.get("Type") or "").lower()
        who = [TAG["caitlin"] if typ in ("renewal", "existing business") else TAG["lola"]]
        has_plus = any(li.get("Product") == "Postscript Plus" for li in bundle.get("line_items", []))
        if has_plus and channel_id != SFDC:
            who.append(TAG["viv"])
        out = out.rstrip() + "\n\ncc " + " ".join(f"<@{w}>" for w in who)
    # Final step: render to Slack-native mrkdwn (table -> monospace code block).
    out = _to_slack(out)
    return out, clean
