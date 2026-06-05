# Closed-Won Contract Audit — n8n automation

Auto-audits every new deal posted to **#closed-won-presales** (`C08JS7N86D6`):
Slack message → Salesforce + SpotDraft data → Claude runs the 18-check audit → reply in-thread.

## What the workflow does
```
Slack Trigger (new message)
  └─ IF: text contains "just closed" + "*Name:*"   (ignore chatter / own replies)
      └─ Parse opp name + thread_ts
          └─ SF Token (client_credentials)
              └─ SF Opp  ── IF found & Closed Won? ──no──> Slack "opp not found, skipped"
                  └─ SF Line Items
                      └─ SF Contact Roles
                          └─ SF SpotDraft_Contract__c (newest)
                              └─ Pick executed contract ── IF none ──> Slack "not yet auditable"
                                  └─ SpotDraft key_pointers (JSON fields)
                                      └─ SpotDraft download PDF → Extract PDF text  (for signer title)
                                          └─ Assemble Claude request (SKILL.md as system prompt)
                                              └─ Claude audit
                                                  └─ Slack post audit (in-thread)
```

## Import
1. n8n → **Workflows → Import from File** → `n8n-closed-won-audit.workflow.json`.
2. Open each red-badged node and attach/confirm credentials (below).
3. Set the environment variables (below), then **Activate** the workflow.

## Credentials & env vars
The workflow reads secrets from n8n **environment variables** (`$env.*`) so nothing sensitive lives in the JSON. Set these on your n8n instance (Settings → Variables, or actual env vars; ensure `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`):

| Env var | Value |
|---|---|
| `SF_INSTANCE_URL` | `https://postscript.my.salesforce.com` |
| `SF_CLIENT_ID` | Salesforce connected-app consumer key (client_credentials) |
| `SF_CLIENT_SECRET` | Salesforce connected-app consumer secret |
| `SPOTDRAFT_CLIENT_ID` | SpotDraft API client-id |
| `SPOTDRAFT_CLIENT_SECRET` | SpotDraft API client-secret |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `AUDIT_MODEL` | `claude-opus-4-8` (or `claude-sonnet-4-6` to cut cost) |
| `AUDIT_SKILL` | **The full text of your SKILL.md** (the closed-won-contract-audit skill) |
| `SLACK_BOT_TOKEN` | Slack bot token (`xoxb-…`) with `chat:write` |

**Slack Trigger node** also needs an n8n **Slack credential** (the app you already have on n8n). The app must:
- be subscribed to the `message.channels` event,
- have scopes `channels:history`, `chat:write`,
- be **invited into #closed-won-presales**.

> Tip: `AUDIT_SKILL` is long. If your n8n caps variable size, instead paste the skill text directly into the `system` field of the **Assemble Claude request** node (replace `$env.AUDIT_SKILL`).

## The two gotchas (built-in handling + what to tune)
1. **Sync lag** — reps post the instant the opp flips to Closed Won, sometimes *before* the executed SO syncs to SpotDraft/SFDC. The flow already branches to a "not yet auditable" reply when no `Status__c = Completed` contract exists. To auto-retry instead of giving up, insert a **Wait** node (e.g. 10 min) on that branch looping back to `SF SpotDraft Contracts`, capped at ~3 tries.
2. **Dedupe** — if the same message re-fires, you'll double-post. Add a check before posting (e.g. `conversations.replies` for an existing audit reply from your bot, or store handled `ts` in a datastore) and skip if already audited.

## Notes / accuracy
- ~15 of the 18 checks come straight from `key_pointers` JSON; the PDF text is included mainly so Claude can read the **signer's title** (check 3) and the opt-out clause wording (check 6). If you want to skip PDF parsing, delete the `SpotDraft download PDF` + `Extract PDF text` nodes and drop `contract_pdf_text` from the assembled bundle — checks 2/3 then rely on `Signatory__c`/contact roles only.
- The audit is **read-only**. It never writes to Salesforce or SpotDraft.
- Renewals / amendments that are still in **Signing** (unexecuted) won't have `Status__c = Completed`, so they correctly fall to the "not yet auditable" branch.
- Node type-versions target a recent n8n; if your version differs, n8n will offer to migrate on import. Verify the IF-node operator UIs after import.
