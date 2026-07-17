# OpsBrain

SMS/WhatsApp operational logging for food bank frontline staff (AISCO Hackathon,
Theme 6 — "Every Team Member an Analyst", built for ACCFB's brief).

Any approved staff member texts (or sends a voice memo to) one number. The
message is structured by one Gemini call into a fixed schema, confirmed by the
sender before anything saves, and stored in Airtable. Leads text `brief` for a
same-day digest; anyone approved texts `find <keyword>` to check whether
something has been reported before. High-urgency confirmed entries alert the
on-duty lead immediately.

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

## Wire up Twilio

**WhatsApp sandbox (works today, free):** Console → Messaging → Try it out →
Send a WhatsApp message → Sandbox settings → "When a message comes in" →
`https://<your-url>/sms`. Every demo phone must first send the `join <code>`
message shown on that page to the sandbox number.

**Real SMS (later):** requires an upgraded Twilio account **and** toll-free
verification of the 833 number (carrier requirement). Then set the number's
Messaging webhook to `/sms` and Voice webhook to `/voice`. The code handles
both channels transparently.

## Demo script

1. Text a new observation → show the confirmation → reply `YES` → show the row
   flip to `confirmed` in Airtable.
2. Text `brief` → digest surfaces the pallet-jack pattern (seeded 3×).
3. Text `find pallet jack` → three matching entries.
4. Text `find tofu` → exactly "No matching entries."
5. Text something high-urgency → `YES` → lead phone gets the alert.

## Design decisions worth saying out loud

- Lookup uses an explicit `find` prefix rather than LLM intent-guessing, so a
  logged observation can never be silently swallowed as a query (determinism
  over cleverness — matches the PRD's "no open-ended chatbot" non-goal).
- Categories are 7 operational buckets (Dock & Receiving, Warehouse &
  Equipment, Food Quality & Produce, Agency & Distribution, Safety,
  Facilities, Other) — swap them in `app.py`/Airtable if ACCFB's own taxonomy
  differs.
- The data steward "UI" is the Airtable base itself, per PRD §9.
