# Closed-Won Contract Audit — Railway worker

The **compute** half of the automation (see `../PRD.md` §9). n8n is the
orchestration layer that listens to Slack; this worker does the heavy lifting:
pull Salesforce + SpotDraft data, run the 16-check audit via Claude using
`SKILL.md` as the system prompt, post the result in-thread, and add a ✅
reaction when the audit is clean.

```
Slack message (new deal alert)
  → n8n Slack Trigger + filter ("*Name:*" present)
      → HTTP POST  {RAILWAY_URL}/audit  { channel_id, thread_ts, text }
          → this worker:  SFDC SOQL → pick contract → SpotDraft key_pointers + PDF
                          → Claude (SKILL.md system prompt) → Slack reply + ✅
```

## Deploy on Railway

1. **New → GitHub Repo →** `huzaifatofeeqctr-stack/audit`.
2. Service **Settings → Root Directory:** `railway-worker`  (so it builds just this folder).
   Branch: `claude/kind-tesla-9l7gw` (or `main` once merged).
3. Nixpacks auto-detects Python from `requirements.txt`; start command is in
   `railway.json` / `Procfile` (`uvicorn main:app --host 0.0.0.0 --port $PORT`).
4. Add the **Variables** below, then **Deploy**.
5. Railway gives the service a public URL — the audit endpoint is `POST <url>/audit`,
   health check `GET <url>/health`.

## Variables (Railway → service → Variables)

| Variable | Value |
|---|---|
| `SF_INSTANCE_URL` | `https://postscript.my.salesforce.com` |
| `SF_CLIENT_ID` | Salesforce connected-app consumer key (client_credentials) |
| `SF_CLIENT_SECRET` | Salesforce connected-app consumer secret |
| `SPOTDRAFT_CLIENT_ID` | SpotDraft API client-id |
| `SPOTDRAFT_CLIENT_SECRET` | SpotDraft API client-secret |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `AUDIT_MODEL` | `claude-opus-4-8` (or a cheaper model to cut cost) |
| `SLACK_BOT_TOKEN` | `xoxb-…` with `chat:write` + `reactions:write` (the app you have on n8n) |
| `SLACK_BOT_USER_ID` | the bot's user id (e.g. `U0B8RPYQ2GL`) — enables dedupe |
| `SKILL_SOURCE` | *(optional)* raw GitHub URL of `SKILL.md`; defaults to this branch's raw URL so prompt edits ship via `git push` with no redeploy |

## Wire up n8n

Replace the heavy SFDC/SpotDraft/Claude nodes with one **HTTP Request** node:
- Trigger: **Slack Trigger** on `message.channels` for both `C08JS7N86D6`
  (#closed-won-presales) and `C0B88KMFJ3E` (#sfdc-oppty-audit).
- IF: text contains `*Name:*` (ignore chatter / the bot's own replies).
- **HTTP Request →** `POST {RAILWAY_URL}/audit` with JSON
  `{ "channel_id": "={{$json.channel}}", "thread_ts": "={{$json.ts}}", "text": "={{$json.text}}" }`.

The worker posts to Slack itself, so n8n needs no further nodes.

## Local test

```bash
pip install -r requirements.txt
export SF_INSTANCE_URL=... SF_CLIENT_ID=... SF_CLIENT_SECRET=... \
       SPOTDRAFT_CLIENT_ID=... SPOTDRAFT_CLIENT_SECRET=... \
       ANTHROPIC_API_KEY=... SLACK_BOT_TOKEN=... AUDIT_MODEL=claude-opus-4-8
uvicorn main:app --reload
# then:
curl -s localhost:8000/audit -H 'content-type: application/json' -d '{
  "channel_id":"C08JS7N86D6","thread_ts":"1781292535.230839",
  "text":"*Name:* Travel Cat | New Business | 6 - 2026"}'
```

## Notes
- **Read-only** against Salesforce and SpotDraft; it only writes to Slack.
- Contract selection / auto-renewal / tagging logic lives in `SKILL.md` (the
  system prompt) — change behavior by editing that file and pushing, not by
  redeploying code.
- Dedupe + "not a deal alert" skips are handled in `main.py`.
