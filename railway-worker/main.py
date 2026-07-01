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
import time
import hmac
import hashlib
import asyncio
import threading
import traceback
from urllib.parse import parse_qs
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import clients
import audit

app = FastAPI(title="closed-won-contract-audit worker")
BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID")

# Serializes the signature scan + backfill so overlapping HTTP triggers (or the
# poller firing while a manual call runs) can't each pass the "already audited?"
# check before any has posted — which would double-post audits into a thread.
_SIG_LOCK = threading.Lock()
VERSION = "0.9.3"  # bump on each deploy to verify GitHub auto-deploy is live

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
_SIG_FIELDS = ("Id, Name, SpotDraft_ID__c, Status__c, Opportunity__c, Opportunity__r.Name, "
               "Account__c, Account__r.Name, Contract_Link__c")
# Document types that should NOT trigger a "sent for signature" notice — these
# aren't auditable Service Orders (Caitlin: NDAs must not post into the channel).
_SKIP_DOC_PATTERNS = ("nondisclosure", "non-disclosure", "non disclosure", "nda",
                      "confidentiality", "dpa", "data processing")


def _is_skippable_doc(contract):
    """True if the signing doc is a non-auditable type (e.g. an NDA) we should
    not announce. Matched on the SpotDraft_Contract__c Name."""
    name = (contract.get("Name") or "").lower()
    return any(p in name for p in _SKIP_DOC_PATTERNS)


# Total Quota Relief lives on the Opportunity under one of a few possible API
# names across orgs; try each (cheaply, guarded) and use the first that resolves.
_QUOTA_FIELDS = ("Total_Quota_Relief_Roll_Up__c", "Total_Quota_Relief__c",
                 "Quota_Relief_Roll_Up__c", "Total_Quota_Relief_Rollup__c")


def _quota_relief(opp_id):
    if not opp_id:
        return None
    for fld in _QUOTA_FIELDS:
        try:
            rows = clients.soql(f"SELECT {fld} FROM Opportunity WHERE Id = '{opp_id}'")
        except Exception:
            continue
        if rows:
            return rows[0].get(fld)  # field exists (value may be 0/empty)
    return None


def _fmt_money(v):
    try:
        f = float(v)
        return "${:,.0f}".format(f) if f == int(f) else "${:,.2f}".format(f)
    except (TypeError, ValueError):
        return str(v)


def _open_opp_by_shop(shop_id):
    """Auto-link by Shop ID (Caitlin's method): the OPEN opp for this shop. A
    signing contract precedes close, so its opp is open (IsClosed = false). If
    several are open, take the most recently created (the active deal) so we link
    in every case rather than falling back to notify-only."""
    if not shop_id:
        return None
    rows = clients.soql(
        f"SELECT {audit.OPP_FIELDS} FROM Opportunity WHERE Shop_ID__c = '{shop_id}' "
        "AND IsClosed = false ORDER BY CreatedDate DESC"
    )
    return rows[0] if rows else None


def _open_opp_by_account(account_id):
    """Account-level fallback: the single OPEN opp on the account, or None."""
    if not account_id:
        return None
    rows = clients.soql(
        f"SELECT {audit.OPP_FIELDS} FROM Opportunity WHERE AccountId = '{account_id}' "
        "AND IsClosed = false ORDER BY CreatedDate DESC"
    )
    return rows[0] if len(rows) == 1 else None


def _resolve_opp_record(contract):
    """Resolve the Opportunity a signing contract is tied to, returning a full
    opp record (audit.OPP_FIELDS) or None. Order:
      1. contract.Opportunity__c if populated (the explicit link);
      2. the contract's Shop ID (from SpotDraft key_pointers) -> single open opp
         (Caitlin's method; also disambiguates multi-shop accounts);
      3. the account -> single open opp.
    None at every step (incl. ambiguous 0/>1 matches) => caller posts notify-only."""
    opp_id = contract.get("Opportunity__c")
    if opp_id:
        rows = clients.soql(f"SELECT {audit.OPP_FIELDS} FROM Opportunity WHERE Id = '{opp_id}'")
        if rows:
            return rows[0]
    tid = contract.get("SpotDraft_ID__c")
    shop = None
    if tid:
        try:
            shop = audit.shop_id_from_key_pointers(clients.spotdraft_key_pointers(tid))
        except Exception:
            shop = None
    return _open_opp_by_shop(shop) or _open_opp_by_account(contract.get("Account__c"))


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
        if _is_skippable_doc(c):
            continue  # NDAs / non-auditable docs don't post a notice (Caitlin)
        acct = (c.get("Account__r") or {}).get("Name") or "(unknown account)"
        opp = _resolve_opp_record(c)
        opp_name = opp.get("Name") if opp else None
        owner = ((opp.get("Owner") or {}).get("Name") if opp else None) or "—"
        qr = _quota_relief(opp.get("Id")) if opp else None
        qr_str = _fmt_money(qr) if qr not in (None, "") else "—"
        link = c.get("Contract_Link__c") or ""
        contract_line = f"{tid}" + (f" (<{link}|open in SpotDraft>)" if link else "")
        notice = (
            f":pencil: *Contract sent for signature* — *{acct}*\n"
            f"> *Status:* {c.get('Status__c')}\n"
            f"> *Contract:* {contract_line}\n"
            f"> *Opportunity:* {opp_name or '—'}\n"
            f"> *Opportunity Owner:* {owner}\n"
            f"> *Total Quota Relief:* {qr_str}"
        )
        if not opp_name:
            notice += "\n> _Couldn't auto-link an opportunity — will audit when it closes._"
        try:
            posted = clients.slack_post(SIG_CHANNEL, notice)
            announced.add(tid)
            entry = {"tid": tid, "account": acct, "opp": opp_name, "audited": False}
            if SIG_AUTO_AUDIT and opp:
                try:
                    message, clean = audit.audit_message(
                        f"*Name:* {opp_name}", SIG_CHANNEL, contract_tid=tid, opp_record=opp)
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


def backfill_signatures(limit=200):
    """One-time/idempotent: for signing contracts ALREADY announced (which
    scan_signatures skips forever), thread a PRELIMINARY audit under the existing
    notice when the opp now resolves (e.g. via the new Shop-ID path). Safe to
    re-run — threads that already have an audit are skipped."""
    done = []
    statuses = "','".join(_SIG_STATUSES)
    try:
        contracts = clients.soql(
            f"SELECT {_SIG_FIELDS} FROM SpotDraft_Contract__c WHERE Status__c IN ('{statuses}') "
            "AND LastModifiedDate = LAST_N_DAYS:1 ORDER BY LastModifiedDate DESC"
        )
    except Exception as e:
        return [{"error": str(e)[:200]}]

    # map each announced T-id -> the notice's ts (to thread under it)
    notice_ts = {}
    try:
        for m in clients.slack_history(SIG_CHANNEL, limit):
            t = m.get("text", "") or ""
            if "sent for signature" in t:
                for tid in re.findall(r"T-\d+", t):
                    notice_ts.setdefault(tid, m.get("ts"))
    except Exception as e:
        return [{"error": str(e)[:200]}]

    for c in contracts:
        tid = c.get("SpotDraft_ID__c")
        ts = notice_ts.get(tid)
        if not tid or not ts:
            continue  # not previously announced -> scan_signatures handles it
        if _thread_already_audited(SIG_CHANNEL, ts):
            continue  # already has an audit -> idempotent skip
        opp = _resolve_opp_record(c)
        if not opp:
            done.append({"tid": tid, "resolved": False})
            continue
        try:
            message, clean = audit.audit_message(
                f"*Name:* {opp.get('Name')}", SIG_CHANNEL, contract_tid=tid, opp_record=opp)
            clients.slack_post(SIG_CHANNEL, message, thread_ts=ts)
            if clean:
                clients.slack_react(SIG_CHANNEL, ts, "white_check_mark")
            done.append({"tid": tid, "opp": opp.get("Name"), "audited": True, "clean": clean})
        except Exception as e:
            done.append({"tid": tid, "audit_error": str(e)[:200]})
    return done


@app.post("/scan-signatures")
@app.get("/scan-signatures")
def scan_signatures_endpoint():
    """Manually trigger the signature-stage scan (also runnable via cron)."""
    with _SIG_LOCK:  # serialize with any other scan/backfill run (no double-post)
        return {"ok": True, "result": scan_signatures()}


@app.post("/backfill-signatures")
@app.get("/backfill-signatures")
def backfill_signatures_endpoint():
    """One-time/idempotent backfill: audit already-announced signing contracts
    whose opp now resolves (threads under the existing notice)."""
    with _SIG_LOCK:  # serialize with any other scan/backfill run (no double-post)
        return {"ok": True, "result": backfill_signatures()}


def _delete_duplicate_audits(channel_id, parent_ts):
    """Keep the earliest bot-authored audit reply in a thread, delete the rest.
    Self-scoped: only ever removes our own messages that look like an audit, so
    it can't touch human messages or notices. Returns {kept, deleted}."""
    replies = clients.slack_thread_replies(channel_id, parent_ts)[1:]
    audits = [m for m in replies
              if (not BOT_USER_ID or m.get("user") == BOT_USER_ID)
              and ("checks passed" in (m.get("text") or "") or "Audit:" in (m.get("text") or ""))]
    deleted = []
    for m in audits[1:]:  # keep audits[0] (earliest)
        try:
            clients.slack_delete(channel_id, m["ts"])
            deleted.append(m["ts"])
        except Exception as e:
            deleted.append({"ts": m["ts"], "error": str(e)[:120]})
    return {"kept": audits[0]["ts"] if audits else None, "deleted": deleted}


@app.post("/admin/dedup-thread")
@app.get("/admin/dedup-thread")
def dedup_thread_endpoint(channel_id: str, parent_ts: str, key: str = ""):
    """Remove duplicate bot audit replies under a thread (keep the earliest).
    Guarded by ADMIN_KEY when that env is set; the op is self-scoped to our own
    audit messages regardless."""
    admin_key = os.environ.get("ADMIN_KEY")
    if admin_key and key != admin_key:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    with _SIG_LOCK:
        return {"ok": True, "result": _delete_duplicate_audits(channel_id, parent_ts)}


_AUDIT_FRAGMENT = ("```", "checks passed", "Audit:", "Updates Required",
                   "What to fix", "PRELIMINARY", "Result:", "*Links:*", "Links:")


def _cleanup_old_audits(channel_id, parent_ts):
    """Keep only the NEWEST bot audit message in a thread; delete older
    audit-like bot fragments (old multi-message/table audits left stray bits that
    the narrower dedup filter missed). Self-scoped to our own messages."""
    replies = clients.slack_thread_replies(channel_id, parent_ts)
    frags = [m for m in (replies[1:] if replies else [])
             if (not BOT_USER_ID or m.get("user") == BOT_USER_ID)
             and any(s in (m.get("text") or "") for s in _AUDIT_FRAGMENT)]
    if len(frags) <= 1:
        return {"parent_ts": parent_ts, "kept": frags[0]["ts"] if frags else None, "deleted": 0}
    frags.sort(key=lambda m: float(m["ts"]))
    deleted = 0
    for m in frags[:-1]:  # keep the most recent (the fresh audit)
        try:
            clients.slack_delete(channel_id, m["ts"])
            deleted += 1
        except Exception:
            pass
    return {"parent_ts": parent_ts, "kept": frags[-1]["ts"], "deleted": deleted}


@app.post("/admin/cleanup-thread")
@app.get("/admin/cleanup-thread")
def cleanup_thread_endpoint(channel_id: str, parent_ts: str, key: str = ""):
    """Remove stray old audit fragments in a thread, keeping only the newest
    audit. Self-scoped to our own messages; ADMIN_KEY-guarded when set."""
    admin_key = os.environ.get("ADMIN_KEY")
    if admin_key and key != admin_key:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    with _SIG_LOCK:
        return {"ok": True, "result": _cleanup_old_audits(channel_id, parent_ts)}


def _reformat_thread(channel_id, parent_ts):
    """One-time cleanup: replace the old table-format bot audit replies in a
    thread with a single fresh (current-format) audit, and ✅ the parent if clean.
    Re-resolves the exact opp + contract from the old reply (Opportunity link +
    T-id), so it works for open/signature-stage opps too. Re-audits BEFORE
    deleting, so a failure leaves the existing replies untouched."""
    replies = clients.slack_thread_replies(channel_id, parent_ts)
    audits = [m for m in (replies[1:] if replies else [])
              if (not BOT_USER_ID or m.get("user") == BOT_USER_ID)
              and ("checks passed" in (m.get("text") or "") or "Audit:" in (m.get("text") or ""))]
    if not audits:
        return {"parent_ts": parent_ts, "skipped": "no existing audit reply"}
    txt = "\n".join(m.get("text", "") for m in audits)
    mo = re.search(r"/Opportunity/([A-Za-z0-9]{15,18})", txt)
    if not mo:
        return {"parent_ts": parent_ts, "skipped": "no opp id in old audit"}
    opp_id = mo.group(1)
    # Only PIN the prior contract when the old audit was signature-stage
    # (PRELIMINARY) — there we must re-audit the same signing SO. For an executed
    # deal, DON'T pin: let contract selection re-pick (now SOW-aware), so a recheck
    # on an Upsell correctly moves to its SOW instead of re-using the base SO the
    # old audit wrongly chose.
    tid = None
    if "PRELIMINARY" in txt:
        mt = re.search(r"T-\d+", txt) or re.search(r"/contracts/v2/(\d+)", txt)
        if mt:
            tid = mt.group(0) if mt.group(0).startswith("T-") else f"T-{mt.group(1)}"
    rows = clients.soql(f"SELECT {audit.OPP_FIELDS} FROM Opportunity WHERE Id = '{opp_id}'")
    if not rows:
        return {"parent_ts": parent_ts, "skipped": f"opp {opp_id} not found"}
    opp = rows[0]
    message, clean = audit.audit_message(
        f"*Name:* {opp.get('Name')}", channel_id, contract_tid=tid, opp_record=opp)
    deleted = 0
    for m in audits:
        try:
            clients.slack_delete(channel_id, m["ts"])
            deleted += 1
        except Exception:
            pass
    clients.slack_post(channel_id, message, thread_ts=parent_ts)
    # Sync the ✅ on the MAIN message: add it when clean, clear a stale one when
    # a re-audit is no longer clean (so the check mark always reflects current state).
    react = (clients.slack_react(channel_id, parent_ts, "white_check_mark") if clean
             else clients.slack_unreact(channel_id, parent_ts, "white_check_mark"))
    return {"parent_ts": parent_ts, "opp": opp.get("Name"), "tid": tid,
            "clean": clean, "deleted": deleted, "react": react}


@app.post("/admin/reformat-thread")
@app.get("/admin/reformat-thread")
def reformat_thread_endpoint(channel_id: str, parent_ts: str, key: str = ""):
    """One-time: swap a thread's old table-format audit(s) for a single fresh one
    and ✅ the parent if clean. Self-scoped to our own audit replies. ADMIN_KEY-
    guarded when that env is set."""
    admin_key = os.environ.get("ADMIN_KEY")
    if admin_key and key != admin_key:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    with _SIG_LOCK:
        return {"ok": True, "result": _reformat_thread(channel_id, parent_ts)}


_REAUDIT_KW = ("recheck", "re-audit", "reaudit", "re audit", "audit again",
               "re-run", "rerun", "check again", "fixed", "re audit", "audit this")
_AUDIT_TEXT = ("checks passed", "Audit:", "Updates Required", "Clean — nothing")


def _reaudit_requested(channel_id, parent_ts):
    """True if a human posted a re-audit keyword in the thread AFTER our most
    recent audit reply (so a rep who fixed SFDC can ask for a fresh check).
    Timestamp-based, so it never re-fires on its own — only a NEW keyword reply
    newer than the latest audit triggers it."""
    replies = clients.slack_thread_replies(channel_id, parent_ts)[1:]
    last_audit = 0.0
    for m in replies:
        if (not BOT_USER_ID or m.get("user") == BOT_USER_ID) and \
                any(s in (m.get("text") or "") for s in _AUDIT_TEXT):
            last_audit = max(last_audit, float(m.get("ts") or 0))
    if not last_audit:
        return False  # no audit yet — fresh-audit path handles it
    for m in replies:
        if BOT_USER_ID and m.get("user") == BOT_USER_ID:
            continue
        if float(m.get("ts") or 0) <= last_audit:
            continue
        if any(k in (m.get("text") or "").lower() for k in _REAUDIT_KW):
            return True
    return False


def scan_reaudits(limit=200, fresh_window=10800):
    """Re-audit threads where a rep asked for a recheck via a keyword reply in the
    thread ('recheck' / 'reaudit' / 'fixed' / 'check again' …). Repeatable and
    timestamp-deduped (only a NEW keyword reply newer than the last audit fires).
    Reuses _reformat_thread: re-resolves the same opp + contract, re-runs, replaces
    the prior audit, and syncs the ✅ on the main message (add if clean, clear if not).

    Looks back `limit` top-level messages per channel (~days of history) so a
    recheck on an older deal still lands, but only opens threads whose newest reply
    is within `fresh_window` seconds — a keyword reply is recent, so this stays
    cheap (we don't re-read every old thread each poll).

    Reactions are deliberately NOT used as a trigger: Slack only lets a bot remove
    its OWN reaction (never the rep's), so a reaction trigger either re-fires forever
    or forces the bot to add its own duplicate marker — both bad. The keyword reply
    adds zero reaction clutter."""
    done = []
    cutoff = time.time() - fresh_window
    for ch in AUDIT_CHANNELS:
        try:
            msgs = clients.slack_history(ch, limit)
        except Exception as e:
            done.append({"channel": ch, "error": str(e)})
            continue
        for msg in msgs:
            ts = msg.get("ts")
            if not ts or not msg.get("reply_count"):
                continue
            # only open threads with a RECENT reply (a new keyword reply is recent),
            # so an older parent is still covered without reading every old thread
            if float(msg.get("latest_reply") or 0) < cutoff:
                continue
            try:
                if _reaudit_requested(ch, ts):
                    with _SIG_LOCK:
                        r = _reformat_thread(ch, ts)
                    done.append({"channel": ch, "parent_ts": ts, "reaudit": r})
            except Exception as e:
                done.append({"channel": ch, "parent_ts": ts, "error": str(e)[:200]})
    return done


@app.post("/scan-reaudits")
@app.get("/scan-reaudits")
def scan_reaudits_endpoint():
    """Sweep both channels for rep-requested re-audits (keyword reply)."""
    return {"ok": True, "result": scan_reaudits()}


def react_clean_backlog(limit=60):
    """Add the ✅ to the MAIN message of every thread whose latest audit is clean
    (idempotent — already_reacted is fine). One-time backfill after reactions:write
    was granted; new clean audits ✅ themselves at post time."""
    done = []
    for ch in AUDIT_CHANNELS:
        try:
            msgs = clients.slack_history(ch, limit)
        except Exception as e:
            done.append({"channel": ch, "error": str(e)})
            continue
        for msg in msgs:
            ts = msg.get("ts")
            if not ts or not msg.get("reply_count"):
                continue
            replies = clients.slack_thread_replies(ch, ts)[1:]
            audits = [m for m in replies
                      if (not BOT_USER_ID or m.get("user") == BOT_USER_ID)
                      and "checks passed" in (m.get("text") or "")]
            if not audits:
                continue
            latest = max(audits, key=lambda m: float(m.get("ts") or 0)).get("text") or ""
            if "— clean" in latest or "Clean — nothing" in latest:
                r = clients.slack_react(ch, ts, "white_check_mark")
                done.append({"channel": ch, "parent_ts": ts, "react_ok": r.get("ok"),
                             "error": r.get("error")})
    return done


@app.post("/admin/react-clean-backlog")
@app.get("/admin/react-clean-backlog")
def react_clean_backlog_endpoint(key: str = ""):
    """One-time: ✅ the main message of all already-posted clean audits."""
    admin_key = os.environ.get("ADMIN_KEY")
    if admin_key and key != admin_key:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return {"ok": True, "result": react_clean_backlog()}


@app.post("/admin/unreact")
@app.get("/admin/unreact")
def unreact_endpoint(channel_id: str, parent_ts: str, emoji: str = "white_check_mark", key: str = ""):
    """Remove one of the bot's OWN reactions from a message (e.g. clear a stray
    :retweet: marker the bot added). Cannot remove other users' reactions."""
    admin_key = os.environ.get("ADMIN_KEY")
    if admin_key and key != admin_key:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return {"ok": True, "result": clients.slack_unreact(channel_id, parent_ts, emoji)}


@app.post("/reaudit-thread")
@app.get("/reaudit-thread")
def reaudit_thread_endpoint(channel_id: str, parent_ts: str):
    """Manually re-audit one thread now (re-resolves opp+contract, replaces the
    prior audit, ✅ if clean)."""
    with _SIG_LOCK:
        return {"ok": True, "result": _reformat_thread(channel_id, parent_ts)}


# ----- /reaudit slash command: on-demand audit by opp link / name / T-id -----
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")


def _verify_slack(body_bytes, headers):
    """Verify a Slack slash-command request (HMAC of 'v0:ts:body'). If no signing
    secret is configured, allow through so it can be set up/tested first."""
    if not SLACK_SIGNING_SECRET:
        return True
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    try:
        if not ts or not sig or abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False
    base = b"v0:" + ts.encode() + b":" + body_bytes
    mine = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mine, sig)


def _audit_target(channel_id, text):
    """Resolve an opp from free text (Salesforce opp link/Id, SpotDraft link/T-id,
    or an opp name) and post a fresh audit to the channel. Returns a short status."""
    text = (text or "").strip()
    opp, tid = None, None
    m = re.search(r"/Opportunity/([A-Za-z0-9]{15,18})", text) or re.search(r"\b(006[A-Za-z0-9]{12,15})\b", text)
    if m:
        rows = clients.soql(f"SELECT {audit.OPP_FIELDS} FROM Opportunity WHERE Id = '{m.group(1)}'")
        opp = rows[0] if rows else None
    if not opp:
        mt = re.search(r"T-\d+", text) or re.search(r"/contracts/v2/(\d+)", text)
        if mt:
            tid = mt.group(0) if mt.group(0).startswith("T-") else f"T-{mt.group(1)}"
            crows = clients.soql(f"SELECT {_SIG_FIELDS} FROM SpotDraft_Contract__c WHERE SpotDraft_ID__c = '{tid}'")
            if crows:
                opp = _resolve_opp_record(crows[0])
    name = (opp.get("Name") if opp else None) or text
    if opp:
        message, clean = audit.audit_message(f"*Name:* {name}", channel_id, contract_tid=tid, opp_record=opp)
    else:                                  # fall back to name lookup (Closed-Won/Stage-5)
        message, clean = audit.audit_message(f"*Name:* {name}", channel_id)
    posted = clients.slack_post(channel_id, message)
    if clean:
        clients.slack_react(channel_id, posted.get("ts"), "white_check_mark")
    return {"opp": name, "clean": clean}


@app.post("/slack/command")
async def slack_command(req: Request):
    """Slack slash command (e.g. `/reaudit <opp link | name | T-id>`). Acks within
    Slack's 3s window, then audits in the background and posts the result in-channel."""
    raw = await req.body()
    if not _verify_slack(raw, req.headers):
        return JSONResponse({"text": "signature verification failed"}, status_code=401)
    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    channel_id = form.get("channel_id")
    text = (form.get("text") or "").strip()
    if not text:
        return {"response_type": "ephemeral",
                "text": "Usage: `/reaudit <opportunity link, name, or T-id>`"}

    def _bg():
        try:
            with _SIG_LOCK:
                _audit_target(channel_id, text)
        except Exception as e:
            try:
                clients.slack_post(channel_id, f":warning: `/reaudit` error: `{str(e)[:200]}`")
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True).start()
    return {"response_type": "ephemeral",
            "text": f":mag: Auditing *{text[:80]}*… the result will post in this channel shortly."}


def _reaudit_or_audit_thread(channel_id, parent_ts):
    """Re-audit a specific thread: if it already has an audit, refresh it
    (replace + ✅ sync); otherwise, if the parent is a Rattle deal alert, audit it
    fresh in-thread. Used by the 'Re-audit this deal' message shortcut so a recheck
    is tied to the exact deal — works inside threads (slash commands can't)."""
    r = _reformat_thread(channel_id, parent_ts)
    if not r.get("skipped"):
        return r
    msgs = clients.slack_thread_replies(channel_id, parent_ts)
    if not msgs:
        return r
    text = _flatten_blocks(msgs[0])
    if not audit.parse_opp_name(text):
        return {"parent_ts": parent_ts, "skipped": "no audit to refresh and parent isn't a deal alert"}
    message, clean = audit.audit_message(text, channel_id)
    clients.slack_post(channel_id, message, thread_ts=parent_ts)
    if clean:
        clients.slack_react(channel_id, parent_ts, "white_check_mark")
    return {"parent_ts": parent_ts, "fresh": True, "clean": clean}


@app.post("/slack/interactivity")
async def slack_interactivity(req: Request):
    """Slack interactivity (message shortcuts). The 'Re-audit this deal' shortcut
    fires on a specific message; we re-audit THAT thread (case-specific, works in
    threads). Acks empty within 3s and does the work in the background."""
    import json as _json
    raw = await req.body()
    if not _verify_slack(raw, req.headers):
        return JSONResponse({"text": "signature verification failed"}, status_code=401)
    form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
    try:
        payload = _json.loads(form.get("payload", "{}"))
    except ValueError:
        return JSONResponse({})
    if payload.get("type") == "message_action":
        ch = (payload.get("channel") or {}).get("id")
        msg = payload.get("message") or {}
        parent_ts = msg.get("thread_ts") or msg.get("ts")
        if ch and parent_ts:
            def _bg():
                try:
                    with _SIG_LOCK:
                        _reaudit_or_audit_thread(ch, parent_ts)
                except Exception as e:
                    try:
                        clients.slack_post(ch, f":warning: re-audit error: `{str(e)[:200]}`", thread_ts=parent_ts)
                    except Exception:
                        pass
            threading.Thread(target=_bg, daemon=True).start()
    return JSONResponse({})


def _flag(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _enabled(name):
    """Background loops are ON by default; disable only by explicitly setting the
    env var to a falsey value (0/false/no/off)."""
    return os.environ.get(name, "true").strip().lower() not in ("0", "false", "no", "off", "")


@app.on_event("startup")
async def _start_poller():
    if _enabled("SCAN_ENABLED") or _enabled("SIG_NOTIFY_ENABLED"):
        asyncio.create_task(_poller())
    if _enabled("SCAN_ENABLED"):
        asyncio.create_task(_reaudit_poller())


async def _poller():
    """Main loop: discover new deals to audit + post signature notices."""
    interval = int(os.environ.get("SCAN_INTERVAL", "300"))
    while True:
        if _enabled("SCAN_ENABLED"):
            try:
                await asyncio.to_thread(scan_once)
            except Exception:
                traceback.print_exc()
        if _enabled("SIG_NOTIFY_ENABLED"):
            try:
                await asyncio.to_thread(_locked_scan_signatures)
            except Exception:
                traceback.print_exc()
        await asyncio.sleep(interval)


async def _reaudit_poller():
    """Fast loop so a rep's `recheck` reply is picked up near-real-time (default
    45s) instead of waiting on the 5-minute main poll. Both channels."""
    interval = int(os.environ.get("REAUDIT_INTERVAL", "45"))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(scan_reaudits)
        except Exception:
            traceback.print_exc()


def _locked_scan_signatures():
    with _SIG_LOCK:  # serialize with manual /scan-signatures and /backfill calls
        return scan_signatures()


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
