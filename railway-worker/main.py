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
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import clients
import audit

app = FastAPI(title="closed-won-contract-audit worker")
BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID")
VERSION = "0.4.2"  # bump on each deploy to verify GitHub auto-deploy is live


@app.get("/health")
def health():
    return {"ok": True, "service": "closed-won-contract-audit", "version": VERSION}


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
