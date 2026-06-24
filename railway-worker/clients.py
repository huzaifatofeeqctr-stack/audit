"""Thin clients for Salesforce, SpotDraft, Slack, and PDF text extraction.

All secrets come from environment variables (set them in the Railway service):
  SF_INSTANCE_URL, SF_CLIENT_ID, SF_CLIENT_SECRET     (Salesforce client_credentials)
  SPOTDRAFT_CLIENT_ID, SPOTDRAFT_CLIENT_SECRET         (SpotDraft Public API headers)
  SLACK_BOT_TOKEN                                      (xoxb- token, chat:write + reactions:write)
"""
import os
import re
import time
import zlib
import requests

SF_INSTANCE_URL = os.environ.get("SF_INSTANCE_URL", "https://postscript.my.salesforce.com")
SF_API = "v64.0"


# ---------------------------------------------------------------- Salesforce
_sf_token_cache = {"token": None, "instance": None, "exp": 0}


def sf_token():
    """client_credentials OAuth; cached until ~just before expiry."""
    if _sf_token_cache["token"] and time.time() < _sf_token_cache["exp"]:
        return _sf_token_cache["token"], _sf_token_cache["instance"]
    r = requests.post(
        f"{SF_INSTANCE_URL}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["SF_CLIENT_ID"],
            "client_secret": os.environ["SF_CLIENT_SECRET"],
        },
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    _sf_token_cache.update(
        token=j["access_token"],
        instance=j.get("instance_url", SF_INSTANCE_URL),
        exp=time.time() + 60 * 90,  # tokens last ~2h; refresh well before
    )
    return _sf_token_cache["token"], _sf_token_cache["instance"]


def sf_whoami():
    """Identify the user the connected app authenticates as (client_credentials
    Run-As user). Helps confirm permission grants are on the right user."""
    token, instance = sf_token()
    r = requests.get(
        f"{instance}/services/oauth2/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if not r.ok:
        return {"error": f"{r.status_code}: {r.text[:300]}"}
    j = r.json()
    return {k: j.get(k) for k in ("name", "preferred_username", "email", "user_id", "organization_id")}


def soql(query):
    token, instance = sf_token()
    r = requests.get(
        f"{instance}/services/data/{SF_API}/query",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query},
        timeout=60,
    )
    if not r.ok:
        # surface the Salesforce error body (e.g. INVALID_FIELD / FLS) instead
        # of a bare "400 Client Error"
        raise RuntimeError(f"SF query {r.status_code}: {r.text[:500]} | q={query[:300]}")
    return r.json()["records"]


# ----------------------------------------------------------------- SpotDraft
SPOTDRAFT_BASE = "https://api.spotdraft.com/api/v2"


def _spotdraft_headers():
    return {
        "client-id": os.environ["SPOTDRAFT_CLIENT_ID"],
        "client-secret": os.environ["SPOTDRAFT_CLIENT_SECRET"],
    }


def spotdraft_key_pointers(tid):
    r = requests.get(
        f"{SPOTDRAFT_BASE}/public/contracts/{tid}/key_pointers/",
        headers=_spotdraft_headers(),
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def spotdraft_pdf_text(tid):
    """Download the signed PDF and return extracted text (handles FlateDecode)."""
    r = requests.post(
        f"{SPOTDRAFT_BASE}/public/contracts/{tid}/download/",
        headers={**_spotdraft_headers(), "Content-Type": "application/json"},
        data=b"{}",
        timeout=90,
    )
    r.raise_for_status()
    body = r.content
    # download endpoint may return JSON with a URL, or the bytes directly
    if body[:1] in (b"{", b"["):
        try:
            meta = r.json()
            url = meta.get("url") or meta.get("download_url") or meta.get("file")
            if url:
                body = requests.get(url, timeout=90).content
        except ValueError:
            pass
    return extract_pdf_text(body)


def extract_pdf_text(data: bytes) -> str:
    """Decompress FlateDecode streams and pull parenthesized text (no poppler needed)."""
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            d = zlib.decompress(m.group(1))
        except Exception:
            continue
        seg = []
        for t in re.finditer(rb"\((?:\\.|[^()\\])*\)", d):
            s = re.sub(rb"\\([()\\])", rb"\1", t.group(0)[1:-1])
            seg.append(s)
        if seg:
            out.append(b" ".join(seg))
    text = b"\n".join(out).decode("latin-1", "replace")
    return re.sub(r"[ \t]+", " ", text)


# --------------------------------------------------------------------- Slack
SLACK_API = "https://slack.com/api"


def slack_post(channel_id, text, thread_ts=None):
    r = requests.post(
        f"{SLACK_API}/chat.postMessage",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        json={"channel": channel_id, "text": text, "thread_ts": thread_ts, "mrkdwn": True},
        timeout=30,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"slack chat.postMessage failed: {j.get('error')}")
    return j


def slack_delete(channel_id, ts):
    """Delete a message. chat.delete can only remove messages our bot authored,
    so this is safe to expose for cleaning up our own duplicate posts."""
    r = requests.post(
        f"{SLACK_API}/chat.delete",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        json={"channel": channel_id, "ts": ts},
        timeout=30,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"slack chat.delete failed: {j.get('error')}")
    return j


def slack_react(channel_id, message_ts, emoji="white_check_mark"):
    """Add a reaction. BEST-EFFORT: never raises. A reaction is a nice-to-have
    cosmetic signal (✅ on clean deals); a failure here — e.g. the bot token
    lacking `reactions:write` (missing_scope), or already_reacted — must never
    break the audit, which has already been posted by the time we react.
    Returns the Slack response dict (with `ok`/`error`) for diagnostics."""
    try:
        r = requests.post(
            f"{SLACK_API}/reactions.add",
            headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
            json={"channel": channel_id, "timestamp": message_ts, "name": emoji},
            timeout=30,
        )
        j = r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    if not j.get("ok") and j.get("error") not in ("already_reacted",):
        # log but don't raise — the audit post already succeeded
        print(f"[slack_react] non-fatal: reactions.add failed: {j.get('error')}")
    return j


def slack_thread_has_bot_reply(channel_id, thread_ts, bot_user_id):
    """Dedupe: true if our bot already replied in this thread."""
    if not bot_user_id:
        return False
    r = requests.get(
        f"{SLACK_API}/conversations.replies",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        params={"channel": channel_id, "ts": thread_ts, "limit": 50},
        timeout=30,
    )
    j = r.json()
    if not j.get("ok"):
        return False
    return any(m.get("user") == bot_user_id for m in j.get("messages", [])[1:])


def slack_history(channel_id, limit=25):
    """Recent messages in a channel (newest first). Needs channels:history."""
    r = requests.get(
        f"{SLACK_API}/conversations.history",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        params={"channel": channel_id, "limit": limit},
        timeout=30,
    )
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"slack conversations.history failed: {j.get('error')}")
    return j.get("messages", [])


def slack_thread_replies(channel_id, thread_ts):
    """All replies (incl. parent) for a thread."""
    r = requests.get(
        f"{SLACK_API}/conversations.replies",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        params={"channel": channel_id, "ts": thread_ts, "limit": 50},
        timeout=30,
    )
    j = r.json()
    return j.get("messages", []) if j.get("ok") else []
