"""
OpsBrain — SMS/WhatsApp operational logging for food bank frontline staff.

One webhook service:
  POST /sms    — inbound SMS or WhatsApp message (Twilio webhook)
  POST /voice  — inbound phone call (Twilio webhook) -> record a voice memo
  POST /voice/handle — recording callback, transcribes + structures the memo
  GET  /health — keep-alive / status

Flows (see PRD):
  F1 log:     any text/voice memo -> Gemini structures it -> pending record ->
              confirmation message -> "YES" confirms -> high urgency alerts leads
  F2 brief:   "brief" -> digest of confirmed entries from last 24h
  F3 lookup:  "find <keywords>" -> keyword match over confirmed entries
  F4 sensitive: flagged content is redirected, never enters the searchable log
Guarantees: nothing auto-saves without confirmation; no fabricated data; caller
ID never appears in any outbound text; raw transcript survives LLM failures.
"""

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response

load_dotenv()

TWILIO_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_SMS_FROM = os.environ["TWILIO_PHONE_NUMBER"]
TWILIO_WA_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
AT_KEY = os.environ["AIRTABLE_API_KEY"]
AT_BASE = os.environ["AIRTABLE_BASE_ID"]
AT_LOG = os.environ.get("AIRTABLE_LOG_TABLE", "Log Entries")
AT_ROSTER = os.environ.get("AIRTABLE_ROSTER_TABLE", "Roster")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
SELF_PING = os.environ.get("SELF_PING", "true").lower() == "true"

CATEGORIES = [
    "Dock & Receiving", "Warehouse & Equipment", "Food Quality & Produce",
    "Agency & Distribution", "Safety", "Facilities", "Other",
]

SENSITIVE_REPLY = ("This sounds like something to raise directly with your "
                   "supervisor — please reach out to them.")
NO_MATCH_REPLY = "No matching entries."

log = logging.getLogger("opsbrain")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI()

# ---------------------------------------------------------------- Airtable

AT_HEADERS = {"Authorization": f"Bearer {AT_KEY}", "Content-Type": "application/json"}


def at_url(table: str) -> str:
    return f"https://api.airtable.com/v0/{AT_BASE}/{quote(table)}"


def at_list(table, formula=None, max_records=100, sort_desc_by=None):
    params = {"maxRecords": max_records}
    if formula:
        params["filterByFormula"] = formula
    if sort_desc_by:
        params["sort[0][field]"] = sort_desc_by
        params["sort[0][direction]"] = "desc"
    r = requests.get(at_url(table), headers=AT_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["records"]


def at_create(table, fields):
    r = requests.post(at_url(table), headers=AT_HEADERS,
                      json={"fields": fields, "typecast": True}, timeout=15)
    r.raise_for_status()
    return r.json()


def at_update(table, record_id, fields):
    r = requests.patch(f"{at_url(table)}/{record_id}", headers=AT_HEADERS,
                       json={"fields": fields, "typecast": True}, timeout=15)
    r.raise_for_status()
    return r.json()


_roster_cache = {"at": 0.0, "rows": []}


def get_roster():
    if time.time() - _roster_cache["at"] > 60:
        rows = [r["fields"] for r in at_list(AT_ROSTER, max_records=100)]
        _roster_cache.update(at=time.time(), rows=rows)
    return _roster_cache["rows"]


def is_allowed(phone: str) -> bool:
    return any(r.get("phone_number") == phone and r.get("active")
               for r in get_roster())


def lead_numbers():
    return [r["phone_number"] for r in get_roster()
            if r.get("active") and "lead" in (r.get("role") or "").lower()]

# ---------------------------------------------------------------- Gemini

STRUCTURE_RULES = f"""You structure observations reported by food bank frontline staff \
(dock, receiving, warehouse, drivers) into a fixed schema.

Rules:
- summary: one plain-language sentence restating the observation. No embellishment.
- category: one of {CATEGORIES}.
- urgency: high only if it blocks operations or is a safety risk in the next ~48h.
- stated_impact: ONLY a number/impact the sender explicitly stated (e.g. "3 pallets", \
"second time this week"). If none stated, null. NEVER infer or invent one.
- the_ask: the suggested action. If the sender gave none, write the most direct \
plain restatement of what needs attention — do not invent specifics.
- sensitive_flag: true ONLY for safety violations against people, interpersonal \
conflict, harassment, or HR-sensitive content. Broken equipment / trip hazards are \
NOT sensitive; they are normal operational reports."""

STRUCT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transcript": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "category": {"type": "STRING", "enum": CATEGORIES},
        "urgency": {"type": "STRING", "enum": ["low", "medium", "high"]},
        "stated_impact": {"type": "STRING", "nullable": True},
        "the_ask": {"type": "STRING"},
        "sensitive_flag": {"type": "BOOLEAN"},
    },
    "required": ["summary", "category", "urgency", "stated_impact",
                 "the_ask", "sensitive_flag"],
}


def gemini_structure(text=None, audio_bytes=None, audio_mime=None, correction=None):
    """One structuring call. Returns dict per STRUCT_SCHEMA. Raises on failure."""
    parts = []
    if audio_bytes:
        parts.append({"text": STRUCTURE_RULES +
                      "\n\nFirst transcribe the attached voice memo verbatim into "
                      "'transcript', then structure it."})
        parts.append({"inline_data": {
            "mime_type": audio_mime or "audio/ogg",
            "data": base64.b64encode(audio_bytes).decode(),
        }})
    else:
        prompt = STRUCTURE_RULES + f"\n\nTranscript: \"{text}\""
        if correction:
            prompt += (f"\n\nThe sender sent a correction to the above: "
                       f"\"{correction}\". Re-structure taking the correction "
                       f"as authoritative.")
        parts.append({"text": prompt})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "responseSchema": STRUCT_SCHEMA,
        },
    }
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
        json=body, timeout=60)
    r.raise_for_status()
    cand = r.json()["candidates"][0]
    text_out = "".join(p.get("text", "") for p in cand["content"]["parts"]
                       if not p.get("thought"))
    return json.loads(text_out)

# ---------------------------------------------------------------- Twilio out

def send_message(to: str, body: str, whatsapp: bool):
    """Outbound send via REST (used for lead alerts + voice-memo confirmations)."""
    from_ = TWILIO_WA_FROM if whatsapp else TWILIO_SMS_FROM
    to_ = f"whatsapp:{to}" if whatsapp and not to.startswith("whatsapp:") else to
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={"From": from_, "To": to_, "Body": body}, timeout=15)
    if r.status_code >= 400:
        log.error("Twilio send failed %s: %s", r.status_code, r.text[:300])
    return r


def twiml_message(body: str) -> Response:
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
           f"<Message>{escape(body)}</Message></Response>")
    return Response(content=xml, media_type="text/xml")


def twiml_empty() -> Response:
    return Response(content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response/>",
                    media_type="text/xml")

# ---------------------------------------------------------------- record ops

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_pending(sender: str):
    recs = at_list(
        AT_LOG,
        formula=f"AND({{sender_phone}}='{sender}', {{status}}='pending_confirmation')",
        max_records=1, sort_desc_by="timestamp")
    return recs[0] if recs else None


def create_entry(sender, raw, structured=None, error_note=None):
    fields = {
        "timestamp": now_iso(),
        "sender_phone": sender,
        "raw_transcript": raw,
        "status": "pending_confirmation",
    }
    if structured:
        fields.update(
            summary=structured["summary"],
            category=structured["category"],
            urgency=structured["urgency"],
            the_ask=structured["the_ask"],
            sensitive_flag=bool(structured["sensitive_flag"]),
        )
        if structured.get("stated_impact"):
            fields["stated_impact"] = structured["stated_impact"]
        if structured["sensitive_flag"]:
            fields["status"] = "sensitive_redirect"
    if error_note:
        fields["error_note"] = error_note[:500]
    return at_create(AT_LOG, fields)


def confirmation_text(structured):
    return (f"Got it: {structured['summary']} "
            f"[{structured['category']} / {structured['urgency']}] "
            f"Reply YES to save, NO to discard, or reply to correct.")


def handle_new_observation(sender, raw, audio=None, audio_mime=None):
    try:
        structured = gemini_structure(text=None if audio else raw,
                                      audio_bytes=audio, audio_mime=audio_mime)
    except Exception as e:
        log.exception("structuring failed")
        create_entry(sender, raw or "[voice memo — transcription failed]",
                     error_note=f"structuring failed: {e}")
        return ("Couldn't auto-process that, but your note is saved for review "
                "as-is. Reply YES to keep it, or resend.")
    raw_text = structured.get("transcript") or raw
    create_entry(sender, raw_text, structured=structured)
    if structured["sensitive_flag"]:
        return SENSITIVE_REPLY
    return confirmation_text(structured)


def handle_confirm(sender, whatsapp):
    pending = get_pending(sender)
    if not pending:
        return "Nothing waiting to confirm. Text an observation to log it."
    at_update(AT_LOG, pending["id"], {"status": "confirmed"})
    f = pending["fields"]
    reply = f"Saved. Logged under {f.get('category', 'uncategorized')}."
    if f.get("urgency") == "high":
        summary, ask = f.get("summary", f.get("raw_transcript", "")), f.get("the_ask", "")
        alerted = 0
        for num in lead_numbers():
            if num == sender:
                continue
            send_message(num, f"URGENT log confirmed: {summary} Ask: {ask}", whatsapp)
            alerted += 1
        reply += (" On-duty lead alerted." if alerted
                  else " (High urgency — you are the on-duty lead on file.)")
    return reply


def handle_discard(sender):
    pending = get_pending(sender)
    if not pending:
        return "Nothing waiting to confirm."
    at_update(AT_LOG, pending["id"], {"status": "discarded"})
    return "Discarded."


def handle_correction(sender, pending, correction_text):
    f = pending["fields"]
    raw = f.get("raw_transcript", "")
    try:
        structured = gemini_structure(text=raw, correction=correction_text)
    except Exception as e:
        log.exception("correction structuring failed")
        at_update(AT_LOG, pending["id"],
                  {"raw_transcript": raw + f"\n[Correction]: {correction_text}",
                   "error_note": f"correction structuring failed: {e}"})
        return ("Couldn't auto-process the correction, but it's saved with your "
                "note. Reply YES to keep as-is.")
    fields = {
        "raw_transcript": raw + f"\n[Correction]: {correction_text}",
        "summary": structured["summary"],
        "category": structured["category"],
        "urgency": structured["urgency"],
        "the_ask": structured["the_ask"],
        "sensitive_flag": bool(structured["sensitive_flag"]),
        "stated_impact": structured.get("stated_impact") or "",
    }
    if structured["sensitive_flag"]:
        fields["status"] = "sensitive_redirect"
        at_update(AT_LOG, pending["id"], fields)
        return SENSITIVE_REPLY
    at_update(AT_LOG, pending["id"], fields)
    return confirmation_text(structured)

# ---------------------------------------------------------------- F2 brief

def handle_brief():
    recs = at_list(
        AT_LOG,
        formula=("AND({status}='confirmed', "
                 "IS_AFTER({timestamp}, DATEADD(NOW(), -24, 'hours')))"),
        max_records=50, sort_desc_by="timestamp")
    if not recs:
        return "No confirmed entries in the last 24h."
    high = [r["fields"] for r in recs if r["fields"].get("urgency") == "high"]
    rest = [r["fields"] for r in recs if r["fields"].get("urgency") != "high"]
    lines = [f"OpsBrain brief — {len(recs)} confirmed in last 24h"]
    if high:
        lines.append("HIGH:")
        for f in high:
            lines.append(f"! {f.get('summary')} Ask: {f.get('the_ask')}")
    if rest:
        lines.append("Also logged:")
        for f in rest[:8]:
            lines.append(f"- [{f.get('category')}] {f.get('summary')}")
        if len(rest) > 8:
            lines.append(f"(+{len(rest) - 8} more in Airtable)")
    return "\n".join(lines)

# ---------------------------------------------------------------- F3 lookup

def handle_lookup(query: str):
    words = [w for w in query.lower().split() if len(w) >= 3]
    if not words:
        return "Send: find <keyword>, e.g. find pallet jack"
    recs = at_list(AT_LOG, formula="{status}='confirmed'",
                   max_records=100, sort_desc_by="timestamp")
    scored = []
    for r in recs:
        f = r["fields"]
        haystack = " ".join([f.get("category", ""), f.get("summary", ""),
                             f.get("the_ask", ""), f.get("raw_transcript", "")]).lower()
        hits = sum(1 for w in words if w in haystack)
        if hits:
            scored.append((hits, f))
    if not scored:
        return NO_MATCH_REPLY
    scored.sort(key=lambda x: x[0], reverse=True)
    lines = [f"{min(len(scored), 3)} of {len(scored)} matching entries:"]
    for _, f in scored[:3]:
        date = (f.get("timestamp") or "")[:10]
        lines.append(f"- [{date}] {f.get('summary')} Ask: {f.get('the_ask')}")
    return "\n".join(lines)

# ---------------------------------------------------------------- webhooks

@app.api_route("/", methods=["GET", "POST"])
@app.api_route("/sms", methods=["GET", "POST"])
async def inbound_sms(request: Request):
    # accept params however Twilio is configured to deliver them
    form = dict(request.query_params)
    if request.method == "POST":
        form.update(await request.form())
    from_raw = form.get("From", "")
    whatsapp = from_raw.startswith("whatsapp:")
    sender = from_raw.removeprefix("whatsapp:")
    body = (form.get("Body") or "").strip()
    num_media = int(form.get("NumMedia") or 0)

    try:
        if not is_allowed(sender):
            log.info("ignored non-roster number")
            return twiml_empty()

        # voice memo sent as media (WhatsApp voice note)
        if num_media > 0 and (form.get("MediaContentType0") or "").startswith("audio"):
            media = requests.get(form["MediaUrl0"], auth=(TWILIO_SID, TWILIO_TOKEN),
                                 timeout=30)
            media.raise_for_status()
            return twiml_message(handle_new_observation(
                sender, raw=None, audio=media.content,
                audio_mime=form.get("MediaContentType0")))

        low = body.lower()
        if low in ("yes", "y", "yes."):
            return twiml_message(handle_confirm(sender, whatsapp))
        if low in ("no", "cancel", "discard"):
            return twiml_message(handle_discard(sender))
        if low in ("brief", "breif", "daily brief"):
            return twiml_message(handle_brief())
        for prefix in ("find ", "lookup ", "search "):
            if low.startswith(prefix):
                return twiml_message(handle_lookup(body[len(prefix):]))
        if low in ("find", "lookup", "search"):
            return twiml_message("Send: find <keyword>, e.g. find pallet jack")
        if not body:
            return twiml_empty()

        pending = get_pending(sender)
        if pending:
            return twiml_message(handle_correction(sender, pending, body))
        return twiml_message(handle_new_observation(sender, raw=body))
    except Exception:
        log.exception("inbound handling failed")
        return twiml_message("Something went wrong on our side — your message "
                             "was not saved. Please resend in a minute.")


@app.post("/voice")
async def inbound_voice(request: Request):
    form = await request.form()
    sender = form.get("From", "").removeprefix("whatsapp:")
    if not is_allowed(sender):
        return Response(
            content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Hangup/></Response>",
            media_type="text/xml")
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
           "<Say>Ops Brain here. After the beep, describe the problem and the ask. "
           "Press pound when done.</Say>"
           "<Record action=\"/voice/handle\" maxLength=\"120\" finishOnKey=\"#\" "
           "playBeep=\"true\"/></Response>")
    return Response(content=xml, media_type="text/xml")


@app.post("/voice/handle")
async def voice_handle(request: Request):
    form = await request.form()
    sender = form.get("From", "")
    rec_url = form.get("RecordingUrl", "")
    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
           "<Say>Thanks. You'll get a text to confirm.</Say></Response>")

    def process():
        try:
            audio = None
            for _ in range(5):  # recording file can lag the callback slightly
                r = requests.get(rec_url + ".mp3", auth=(TWILIO_SID, TWILIO_TOKEN),
                                 timeout=30)
                if r.status_code == 200:
                    audio = r.content
                    break
                time.sleep(2)
            if audio is None:
                raise RuntimeError("recording not retrievable")
            reply = handle_new_observation(sender, raw=None, audio=audio,
                                           audio_mime="audio/mpeg")
            send_message(sender, reply, whatsapp=False)
            # retention: don't keep raw audio around once transcribed
            if form.get("RecordingSid"):
                requests.delete(
                    f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}"
                    f"/Recordings/{form['RecordingSid']}.json",
                    auth=(TWILIO_SID, TWILIO_TOKEN), timeout=15)
        except Exception:
            log.exception("voice processing failed")
            send_message(sender, "Couldn't process that voice memo — please try "
                                 "again or text it instead.", whatsapp=False)

    threading.Thread(target=process, daemon=True).start()
    return Response(content=xml, media_type="text/xml")


@app.get("/health")
def health():
    return {"ok": True, "service": "opsbrain"}


@app.on_event("startup")
def start_self_ping():
    if not (SELF_PING and PUBLIC_BASE_URL):
        return

    def loop():
        while True:
            time.sleep(600)
            try:
                requests.get(f"{PUBLIC_BASE_URL}/health", timeout=10)
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True).start()
