"""FastAPI worker that n8n (or Slack Events) calls to run a contract audit.

POST /audit
  body: { "channel_id": "...", "thread_ts": "...", "text": "<rattle alert text>" }
  -> gathers SFDC + SpotDraft data, runs the SKILL.md audit via Claude,
     posts the result in-thread, and adds a ✅ reaction if the audit is clean.

GET /health -> {"ok": true}

Dedupe: if our bot already replied in the thread, the request is skipped
(set SLACK_BOT_USER_ID to enable).
"""
import os
import re
import asyncio
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import clients
import audit

app = FastAPI(title="closed-won-contract-audit worker")
BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID")
VERSION = "0.6.2"  # bump on each deploy to verify GitHub auto-deploy is live

# --- self-contained Slack polling (no n8n / Slack Events needed) ---
RATTLE_USER = os.environ.get("RATTLE_USER_ID", "U05AA8MBV9B")
AUDIT_CHANNELS = [c.strip() for c in os.environ.get(
    "AUDIT_CHANNELS", "C08JS7N86D6,C0B88KMFJ3E").split(",") if c.strip()]
_AUDIT_SIG = ("checks passed", "Not yet auditable", "not yet auditable",
              "Could not find an auditable", "Outside the SMS 16-check")


def _flatten_blocks(msg):
    """Reassemble Rattle's text from message blocks (deal fields live there,
    not in top-level `text`). Bold runs are wrapped in * so `*Name:*` appears."""
    parts = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            t = n.get("text")
            if isinstance(t, str):
                parts.append(("*" + t + "*") if (n.get("style") or {}).get("bold") else t)
            elif isinstance(t, dict) and isinstance(t.get("text"), str):
                parts.append(t["text"])
            for k in ("blocks", "elements", "attachments", "fields"):
                if n.get(k):
                    walk(n[k])

    if isinstance(msg.get("text"), str):
        parts.append(msg["text"] + "\n")
    walk(msg.get("blocks"))
    walk(msg.get("attachments"))
    return "".join(parts).replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def _is_rattle_deal(msg):
    if msg.get("subtype"):              # joins / edits / deletes
        return False
    if msg.get("user") != RATTLE_USER:  # only Rattle
        return False
    return True


def _thread_already_audited(channel_id, ts):
    """Skip if the thread already has an audit — by our bot OR a prior
    human/MCP-posted audit (text signature). Prevents re-auditing the backlog."""
    for m in clients.slack_thread_replies(channel_id, ts)[1:]:
        if BOT_USER_ID and m.get("user") == BOT_USER_ID:
            return True
        t = m.get("text", "") or ""
        if "Audit:" in t or any(s in t for s in _AUDIT_SIG):
            return True
    return False


def scan_once(limit=25):
    """Poll both channels; audit any un-audited Rattle deal alert. Posts as the bot."""
    done = []
    for ch in AUDIT_CHANNELS:
        try:
            msgs = clients.slack_history(ch, limit)
        except Exception as e:
            done.append({"channel": ch, "error": str(e)})
            continue
        for msg in msgs:
            ts = msg.get("ts")
            if not ts or not _is_rattle_deal(msg):
                continue
            text = _flatten_blocks(msg)
            name = audit.parse_opp_name(text)
            if not name:
                continue
            try:
                if _thread_already_audited(ch, ts):
                    continue
                message, clean = audit.audit_message(text, ch)
                clients.slack_post(ch, message, thread_ts=ts)
                if clean:
                    clients.slack_react(ch, ts, "white_check_mark")
                done.append({"channel": ch, "name": name, "clean": clean})
            except Exception as e:
                traceback.print_exc()
                done.append({"channel": ch, "name": name, "error": str(e)[:200]})
    return done


@app.post("/scan")
@app.get("/scan")
def scan_endpoint():
    """Manually trigger a poll of both channels (also runnable via cron)."""
    return {"ok": True, "audited": scan_once()}


# --- "contract sent for signature" notifier (poll SpotDraft contract status) ---
SIG_CHANNEL = os.environ.get("SIG_CHANNEL", "C0B88KMFJ3E")  # #sfdc-oppty-audit
SIG_AUTO_AUDIT = os.environ.get("SIG_AUTO_AUDIT", "true").lower() in ("1", "true", "yes")
_SIG_STATUSES = ("Signing", "Awaiting Signature")
_SIG_FIELDS = ("Id, SpotDraft_ID__c, Status__c, Opportunity__c, Opportunity__r.Name, "
               "Account__c, Account__r.Name, Contract_Link__c")


def _resolve_opp_via_account(account_id):
    """Best-effort opp for a contract whose Opportunity__c is blank: the single
    in-flight opp on the account. Returns its Name, or None if 0 or >1 match."""
    if not account_id:
        return None
    rows = clients.soql(
        "SELECT Name FROM Opportunity WHERE AccountId = '%s' AND (StageName = 'Closed Won' "
        "OR StageName LIKE '5 -%%' OR StageName = 'Pricing & Negotiations') "
        "AND CloseDate >= LAST_N_DAYS:45 ORDER BY CloseDate DESC" % account_id
    )
    return rows[0]["Name"] if len(rows) == 1 else None


def scan_signatures(limit=100):
    """Poll SpotDraft contracts newly in a signature stage; post a heads-up to
    SIG_CHANNEL and (when the opp resolves) a threaded PRELIMINARY audit.
    Dedup is durable via Slack history (the T-id already announced)."""
    done = []
    statuses = "','".join(_SIG_STATUSES)
    try:
        contracts = clients.soql(
            f"SELECT {_SIG_FIELDS} FROM SpotDraft_Contract__c WHERE Status__c IN ('{statuses}') "
            "AND LastModifiedDate = LAST_N_DAYS:1 ORDER BY LastModifiedDate DESC"
        )
    except Exception as e:
        return [{"error": str(e)[:200]}]

    # already-announced T-ids (durable dedup via Slack)
    announced = set()
    try:
        for m in clients.slack_history(SIG_CHANNEL, limit):
            t = m.get("text", "") or ""
            if "sent for signature" in t:
                announced.update(re.findall(r"T-\d+", t))
    except Exception:
        pass

    for c in contracts:
        tid = c.get("SpotDraft_ID__c")
        if not tid or tid in announced:
            continue
        acct = (c.get("Account__r") or {}).get("Name") or "(unknown account)"
        opp_name = (c.get("Opportunity__r") or {}).get("Name") or _resolve_opp_via_account(c.get("Account__c"))
        link = c.get("Contract_Link__c") or ""
        notice = (
            f":pencil: *Contract sent for signature* — *{acct}*\n"
            f"> *Status:* {c.get('Status__c')}\n"
            f"> *Contract:* {tid}" + (f" (<{link}|open in SpotDraft>)" if link else "") + "\n"
            f"> *Opportunity:* {opp_name or '—'}"
        )
        if not opp_name:
            notice += "\n> _Couldn't auto-link an opportunity — will audit when it closes._"
        try:
            posted = clients.slack_post(SIG_CHANNEL, notice)
            announced.add(tid)
            entry = {"tid": tid, "account": acct, "opp": opp_name, "audited": False}
            if SIG_AUTO_AUDIT and opp_name:
                try:
                    message, clean = audit.audit_message(f"*Name:* {opp_name}", SIG_CHANNEL, contract_tid=tid)
                    clients.slack_post(SIG_CHANNEL, message, thread_ts=posted.get("ts"))
                    if clean:
                        clients.slack_react(SIG_CHANNEL, posted.get("ts"), "white_check_mark")
                    entry["audited"] = True
                    entry["clean"] = clean
                except Exception as e:
                    entry["audit_error"] = str(e)[:200]
            done.append(entry)
        except Exception as e:
            done.append({"tid": tid, "error": str(e)[:200]})
    return done


@app.post("/scan-signatures")
@app.get("/scan-signatures")
def scan_signatures_endpoint():
    """Manually trigger the signature-stage scan (also runnable via cron)."""
    return {"ok": True, "result": scan_signatures()}


def _flag(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


@app.on_event("startup")
async def _start_poller():
    if _flag("SCAN_ENABLED") or _flag("SIG_NOTIFY_ENABLED"):
        asyncio.create_task(_poller())


async def _poller():
    interval = int(os.environ.get("SCAN_INTERVAL", "300"))
    while True:
        if _flag("SCAN_ENABLED"):
            try:
                await asyncio.to_thread(scan_once)
            except Exception:
                traceback.print_exc()
        if _flag("SIG_NOTIFY_ENABLED"):
            try:
                await asyncio.to_thread(scan_signatures)
            except Exception:
                traceback.print_exc()
        await asyncio.sleep(interval)


@app.get("/health")
def health():
    return {"ok": True, "service": "closed-won-contract-audit", "version": VERSION}


@app.get("/whoami")
def whoami():
    """Which Salesforce user does the worker authenticate as? (diagnostic)
    Also reports a masked fingerprint of the configured SF_CLIENT_ID so we can
    confirm which connected app the worker uses (the secret is never returned)."""
    cid = os.environ.get("SF_CLIENT_ID", "")
    fingerprint = (cid[:10] + "…" + cid[-6:]) if len(cid) > 18 else "(unset/short)"
    try:
        info = clients.sf_whoami()
    except Exception as e:
        info = {"error": str(e)}
    return {"sf_client_id_fingerprint": fingerprint, "identity": info}


@app.post("/audit")
async def do_audit(req: Request):
    body = await req.json()
    channel_id = body.get("channel_id")
    thread_ts = body.get("thread_ts") or body.get("ts")
    text = body.get("text", "")
    # dry_run: run the full audit and RETURN the output instead of posting to
    # Slack (used to verify worker output without touching the live channels).
    dry_run = bool(body.get("dry_run"))

    if not channel_id or (not thread_ts and not dry_run):
        return JSONResponse({"ok": False, "error": "channel_id and thread_ts required"}, status_code=400)

    # ignore non-deal chatter
    if "*Name:*" not in text:
        return {"ok": True, "skipped": "not a deal alert"}

    # dedupe (skipped on dry_run so already-audited deals can be re-tested)
    if not dry_run:
        try:
            if clients.slack_thread_has_bot_reply(channel_id, thread_ts, BOT_USER_ID):
                return {"ok": True, "skipped": "already audited"}
        except Exception:
            pass

    try:
        message, clean = audit.audit_message(text, channel_id, sf=body.get("sf"))
        if dry_run:
            return {"ok": True, "clean": clean, "dry_run": True, "message": message}
        clients.slack_post(channel_id, message, thread_ts=thread_ts)
        if clean:
            clients.slack_react(channel_id, thread_ts, "white_check_mark")
        return {"ok": True, "clean": clean}
    except Exception as e:
        traceback.print_exc()
        # never post during dry_run; for real runs surface a SHORT note in-thread
        # (never to the channel root) so failures aren't silent but aren't noisy
        if not dry_run and thread_ts:
            try:
                clients.slack_post(channel_id, f":warning: Audit worker error: `{str(e)[:280]}`", thread_ts=thread_ts)
            except Exception:
                pass
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
