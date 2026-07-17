# OpsBrain

Message-based operational logging for food bank frontline staff (AISCO
Hackathon, Theme 6 — "Every Team Member an Analyst", built for ACCFB's brief).

Any approved staff member sends a text or voice memo to one number/bot. The
message is structured by one Gemini call into a fixed schema, confirmed by the
sender before anything saves, and stored in Airtable. Leads text `brief` for a
same-day digest; anyone approved texts `find <keyword>` to check whether
something has been reported before. High-urgency confirmed entries alert the
on-duty lead immediately.

**Channels:** the backend is channel-agnostic. The live demo runs on a
**Telegram bot** (free, no carrier registration). The same webhook handles
Twilio SMS/WhatsApp unchanged — an adopting org registers a number with
Twilio (~$2/month + A2P registration) and flips one webhook URL; no code
changes. We verified the Twilio path end-to-end up to the point where trial
accounts block free-form replies (carrier/trial policy, not code).

## Commands (from an approved phone)

| You send | You get |
|---|---|
| any observation (text or voice memo) | "Got it: [summary]… Reply YES to save" |
| `YES` | entry confirmed (+ lead alert if urgency high) |
| `NO` / `cancel` | pending entry discarded |
| anything else while one is pending | treated as a correction, new confirmation |
| `brief` | digest of confirmed entries, last 24h |
| `find <keyword>` | up to 3 matching confirmed entries, or exactly "No matching entries." |

Numbers not on the Roster table get **no response at all**.

## Guarantees (from the PRD)

- Nothing auto-saves — every entry needs an explicit YES.
- No fabricated data: `stated_impact` is only filled if the sender said it;
  unmatched lookups return exactly "No matching entries."
- Caller ID is stored internally but never appears in any outbound text.
- If the LLM call fails, the raw transcript still saves (with an error note).
- Sensitive/HR content is redirected to a supervisor and never enters the
  searchable log (status `sensitive_redirect`).
- Voice recordings are deleted from Twilio right after transcription.

## Run locally

```
pip install -r requirements.txt
python seed.py          # one-time: seeds roster + 17 demo entries
uvicorn app:app --port 8000
```

Secrets live in `.env` (never commit it — `.gitignore` covers it).

## Deploy (Render free tier)

1. Push this folder to a GitHub repo (public is fine — no secrets are committed).
2. Render dashboard → New → Web Service → connect the repo (or paste the public
   repo URL). `render.yaml` supplies build/start commands.
3. Set the env vars from your local `.env` in the Render dashboard.
4. After deploy, set `PUBLIC_BASE_URL` to the service URL so the self-ping
   keeps the free instance awake (cold starts would exceed Twilio's 15s
   webhook timeout).

## Wire up a channel

**Telegram (the live demo channel):** create a bot via @BotFather, set
`TELEGRAM_BOT_TOKEN`, then point the bot's webhook at
`https://<your-url>/telegram` (the deploy script's `setWebhook` call includes
a secret token derived from the bot token). Staff are added to the Roster
table as `tg:<chat_id>`.

**Twilio SMS/WhatsApp (production path):** upgrade the Twilio account,
complete A2P/toll-free registration for the number, then set the number's
Messaging webhook to `https://<your-url>/sms` and Voice webhook to `/voice`.
The code already handles both channels — trial accounts can't deliver
free-form replies, which is why the demo uses Telegram.

## Demo script (Telegram)

1. Show the Airtable base — this is the data steward's whole "admin UI."
2. Send a voice memo or text observation → show the confirmation → reply
   `YES` → show the row flip to `confirmed` in Airtable.
3. Send `brief` → digest surfaces the pallet-jack pattern (seeded 3×).
4. Send `find pallet jack` → three matching entries.
5. Send `find tofu` → exactly "No matching entries."
6. Invite a judge to message the bot from their phone → dead silence
   (allowlist enforcement, live).

## Design decisions worth saying out loud

- Lookup uses an explicit `find` prefix rather than LLM intent-guessing, so a
  logged observation can never be silently swallowed as a query (determinism
  over cleverness — matches the PRD's "no open-ended chatbot" non-goal).
- Categories are 7 operational buckets (Dock & Receiving, Warehouse &
  Equipment, Food Quality & Produce, Agency & Distribution, Safety,
  Facilities, Other) — swap them in `app.py`/Airtable if ACCFB's own taxonomy
  differs.
- The data steward "UI" is the Airtable base itself, per PRD §9.
