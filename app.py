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
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
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
GEMINI_KEY_BACKUP = os.environ.get("GEMINI_API_KEY_BACKUP", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
AT_KEY = os.environ["AIRTABLE_API_KEY"]
AT_BASE = os.environ["AIRTABLE_BASE_ID"]
AT_LOG = os.environ.get("AIRTABLE_LOG_TABLE", "Log Entries")
AT_ROSTER = os.environ.get("AIRTABLE_ROSTER_TABLE", "Roster")
AT_ROLES = os.environ.get("AIRTABLE_ROLES_TABLE", "Roles")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
SELF_PING = os.environ.get("SELF_PING", "true").lower() == "true"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

CATEGORIES = [
    "Dock & Receiving", "Warehouse & Equipment", "Food Quality & Produce",
    "Agency & Distribution", "Safety", "Facilities", "Other",
]

SENSITIVE_REPLY = ("This sounds like something to raise directly with your "
                   "supervisor — please reach out to them.")
NO_MATCH_REPLY = "No matching entries."

TYPE_ICON = {"observation": "📝", "completion": "✅", "reminder": "⏰",
             "knowledge": "📌"}
URG_ICON = {"high": "🔴", "medium": "🟠", "low": "🟢"}

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


_roles_cache = {"at": 0.0, "rows": []}


def get_roles():
    """Role definitions from the Roles table (name -> categories owned,
    alert_on_high). Adding a role = adding a row there; tolerated if absent."""
    try:
        if time.time() - _roles_cache["at"] > 60:
            rows = [r["fields"] for r in at_list(AT_ROLES, max_records=50)]
            _roles_cache.update(at=time.time(), rows=rows)
    except Exception:
        log.warning("Roles table unavailable; falling back to alerts_for only")
    return _roles_cache["rows"]


def role_info(role_name):
    rn = (role_name or "").strip().lower()
    for r in get_roles():
        if (r.get("role") or "").strip().lower() == rn:
            return r
    return {}

# ---------------------------------------------------------------- Gemini

STRUCTURE_RULES = f"""You are OpsBrain, the message brain for a food bank's frontline \
operations log. Staff (dock, receiving, warehouse, drivers) are non-technical and \
write in casual natural language. First decide the INTENT of the message:

- "log": they are reporting something — a problem, an observation, completed work, \
a heads-up for the next shift, or a standing practice.
- "brief": they are asking what's going on — a summary, the brief, "what happened \
today", "anything I should know for this shift?".
- "lookup": they are asking whether something was reported before, or searching \
the log ("did anyone report the freezer?", "any issues with dock 2 lately?"). \
Put the 2-4 most distinctive search words in lookup_query.
- "confirm": they agree to save the pending entry (yes / yep / save it / correct).
- "discard": they reject the pending entry (no / cancel / don't save / forget it).
- "correction": ONLY if there is a pending entry AND they are adjusting its details.
- "other": greetings, thanks, jokes, anything unrelated to the log.

For intent "log", ALSO fill the entry fields per the rules below. For any other \
intent set: summary "", category "Other", urgency "low", stated_impact null, \
the_ask "", sensitive_flag false, entry_type "observation", follow_up false.

Entry field rules:
- summary: one plain-language sentence restating the observation. No embellishment.
- category: one of {CATEGORIES}.
- urgency: high only if it blocks operations or is a safety risk in the next ~48h.
- stated_impact: ONLY a number/impact the sender explicitly stated (e.g. "3 pallets", \
"second time this week"). If none stated, null. NEVER infer or invent one.
- the_ask: the suggested action. If the sender gave none, write the most direct \
plain restatement of what needs attention — do not invent specifics.
- sensitive_flag: true ONLY for safety violations against people, interpersonal \
conflict, harassment, or HR-sensitive content. Broken equipment / trip hazards are \
NOT sensitive; they are normal operational reports.
- entry_type: "completion" if the sender reports something was finished, fixed, \
or handled ("the pallet jack got repaired", "this got completed"). "reminder" if \
they ask for something to be checked or done at a later time or by the next shift \
("check this in the morning"). "knowledge" if it is a standing instruction or \
practice going forward ("from next time everyone should check X"). Otherwise \
"observation".
- follow_up: true if someone should check or verify something at a later time."""

STRUCT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {"type": "STRING",
                   "enum": ["log", "brief", "lookup", "confirm", "discard",
                            "correction", "other"]},
        "lookup_query": {"type": "STRING", "nullable": True},
        "transcript": {"type": "STRING"},
        "summary": {"type": "STRING"},
        "category": {"type": "STRING", "enum": CATEGORIES},
        "urgency": {"type": "STRING", "enum": ["low", "medium", "high"]},
        "stated_impact": {"type": "STRING", "nullable": True},
        "the_ask": {"type": "STRING"},
        "sensitive_flag": {"type": "BOOLEAN"},
        "entry_type": {"type": "STRING",
                       "enum": ["observation", "completion", "reminder", "knowledge"]},
        "follow_up": {"type": "BOOLEAN"},
    },
    "required": ["intent", "lookup_query", "summary", "category", "urgency",
                 "stated_impact", "the_ask", "sensitive_flag", "entry_type",
                 "follow_up"],
}


def gemini_structure(text=None, audio_bytes=None, audio_mime=None,
                     correction=None, pending_summary=None):
    """One intent + structuring call. Returns dict per STRUCT_SCHEMA."""
    if pending_summary:
        ctx = (f"\n\nContext: this sender HAS a pending unconfirmed entry: "
               f"\"{pending_summary}\" — confirm/discard/correction refer to it.")
    else:
        ctx = ("\n\nContext: this sender has NO pending entry, so confirm/"
               "discard/correction do not apply; a report is intent \"log\".")
    parts = []
    if audio_bytes:
        parts.append({"text": STRUCTURE_RULES + ctx +
                      "\n\nFirst transcribe the attached voice memo verbatim into "
                      "'transcript', then decide intent and structure it."})
        parts.append({"inline_data": {
            "mime_type": audio_mime or "audio/ogg",
            "data": base64.b64encode(audio_bytes).decode(),
        }})
    else:
        prompt = STRUCTURE_RULES + ctx + f"\n\nMessage: \"{text}\""
        if correction:
            prompt += (f"\n\nThe sender sent a correction to the above: "
                       f"\"{correction}\". Intent is \"log\"; re-structure "
                       f"taking the correction as authoritative.")
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
    # Each model family has its own free-tier quota bucket and the flash
    # endpoints shed load under demand spikes, so walk models (and an optional
    # backup key) until one answers. 429 = quota: skip ahead immediately.
    models = [GEMINI_MODEL] + [m for m in ("gemini-3-flash-preview",
                                           "gemini-3.1-flash-lite",
                                           "gemini-flash-lite-latest")
                               if m != GEMINI_MODEL]
    keys = [GEMINI_KEY] + ([GEMINI_KEY_BACKUP] if GEMINI_KEY_BACKUP else [])
    r, last_err = None, None
    for key in keys:
        for model in models:
            for attempt in range(2):
                try:
                    r = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        headers={"x-goog-api-key": key,
                                 "Content-Type": "application/json"},
                        json=body, timeout=20)
                except requests.RequestException as exc:
                    last_err, r = exc, None
                    log.warning("model %s network error (%s), retrying", model, exc)
                    continue
                if r.status_code == 429:
                    break
                if r.status_code in (500, 502, 503):
                    time.sleep(1.5)
                    continue
                break
            if r is not None and r.status_code < 400:
                break
            log.warning("model %s unavailable (%s), trying next", model,
                        r.status_code if r is not None else last_err)
        if r is not None and r.status_code < 400:
            break
    if r is None:
        raise RuntimeError(f"all Gemini models unreachable: {last_err}")
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


def send_telegram(chat_id, text):
    r = requests.post(f"{TG_API}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=15)
    if r.status_code >= 400:
        log.error("Telegram send failed %s: %s", r.status_code, r.text[:300])
    return r


def send_any(to: str, body: str, whatsapp: bool = False):
    """Route an outbound message by roster address: tg:<chat_id> or a phone number."""
    if to.startswith("tg:"):
        send_telegram(to[3:], body)
    else:
        send_message(to, body, whatsapp)


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


def create_entry(sender, raw, structured=None, error_note=None, related_to=None):
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
            entry_type=structured.get("entry_type") or "observation",
            follow_up=bool(structured.get("follow_up")),
        )
        if structured.get("stated_impact"):
            fields["stated_impact"] = structured["stated_impact"]
        if structured["sensitive_flag"]:
            fields["status"] = "sensitive_redirect"
    if related_to:
        fields["related_to"] = related_to
    if error_note:
        fields["error_note"] = error_note[:500]
    return at_create(AT_LOG, fields)


def find_open_match(text):
    """Best keyword match among open (confirmed, unresolved) entries, for
    linking a completion report to the case it closes."""
    words = {w for w in re.findall(r"[a-z0-9#]+", text.lower()) if len(w) >= 3}
    if not words:
        return None
    recs = at_list(AT_LOG,
                   formula="AND({status}='confirmed', {entry_type}!='knowledge')",
                   max_records=100, sort_desc_by="timestamp")
    best, best_score = None, 0
    for r in recs:
        f = r["fields"]
        hay = " ".join([f.get("summary", ""), f.get("the_ask", ""),
                        f.get("raw_transcript", "")]).lower()
        score = sum(1 for w in words if w in hay)
        if score > best_score:
            best, best_score = r, score
    return best if best_score >= 2 else None


def confirmation_text(structured, resolves=None):
    kind = structured.get("entry_type") or "observation"
    lines = [f"{TYPE_ICON.get(kind, '📝')} {structured['summary']}"]
    if structured.get("the_ask"):
        lines.append(f"👉 {structured['the_ask']}")
    lines.append(f"{URG_ICON.get(structured['urgency'], '')} "
                 f"{structured['urgency']} · {structured['category']}")
    if resolves:
        lines.append(f"🔗 Will close: “{resolves}”")
    lines.append("")
    lines.append("Save it? YES ✅ / NO ❌ — or reply with a fix")
    return "\n".join(lines)


def save_entry_and_reply(sender, raw_text, structured):
    related, resolves = None, None
    if structured.get("entry_type") == "completion":
        match = find_open_match(f"{structured['summary']} {raw_text}")
        if match:
            related = match["id"]
            resolves = match["fields"].get("summary", "")
    create_entry(sender, raw_text, structured=structured, related_to=related)
    if structured["sensitive_flag"]:
        return SENSITIVE_REPLY
    return confirmation_text(structured, resolves=resolves)


USAGE_HINT = ("👋 I'm OpsBrain — the shift log.\n"
              "📝 Tell me what you're seeing → I'll log it\n"
              "📋 “What's the brief?” → today's rundown\n"
              "🔎 “Did anyone report X?” → search past entries\n"
              "🎙 Voice memos work too")


def route_nl(sender, structured, raw_text, whatsapp, pending):
    """Route a classified message to the right flow."""
    intent = structured.get("intent") or "log"
    if intent == "brief":
        return handle_brief()
    if intent == "lookup":
        return handle_lookup(structured.get("lookup_query") or raw_text)
    if intent == "confirm":
        return handle_confirm(sender, whatsapp)
    if intent == "discard":
        return handle_discard(sender)
    if intent == "correction" and pending:
        return handle_correction(sender, pending, raw_text)
    if intent == "log" or (intent == "correction" and not pending):
        return save_entry_and_reply(sender, raw_text, structured)
    return USAGE_HINT


def handle_voice(sender, audio, audio_mime, whatsapp):
    pending = get_pending(sender)
    try:
        structured = gemini_structure(
            audio_bytes=audio, audio_mime=audio_mime,
            pending_summary=pending["fields"].get("summary") if pending else None)
    except Exception as e:
        log.exception("voice structuring failed")
        create_entry(sender, "[voice memo — transcription failed]",
                     error_note=f"structuring failed: {e}")
        return ("Couldn't process that voice memo — it's saved for review. "
                "Reply YES to keep it, or try again.")
    raw_text = structured.get("transcript") or "[voice memo]"
    return route_nl(sender, structured, raw_text, whatsapp, pending)


def handle_confirm(sender, whatsapp):
    pending = get_pending(sender)
    if not pending:
        return ("🤷 Nothing waiting to confirm. Just tell me what's happening "
                "and I'll log it.")
    at_update(AT_LOG, pending["id"], {"status": "confirmed"})
    f = pending["fields"]
    reply = f"✅ Saved — {f.get('category', 'uncategorized')}."

    if f.get("entry_type") == "completion" and f.get("related_to"):
        try:
            at_update(AT_LOG, f["related_to"], {
                "status": "resolved",
                "resolution_note": f"{now_iso()[:10]}: {f.get('summary', '')[:250]}",
            })
            reply += " 🔗 Linked case closed."
        except Exception:
            log.exception("resolving linked case failed")

    # routing: high urgency -> active leads; any urgency -> roster members
    # whose alerts_for includes the category (rules editable in Airtable)
    summary = f.get("summary", f.get("raw_transcript", ""))
    ask, cat, high = f.get("the_ask", ""), f.get("category", ""), f.get("urgency") == "high"
    recipients = set()
    for r in get_roster():
        addr = r.get("phone_number")
        if not (r.get("active") and addr) or addr == sender:
            continue
        ri = role_info(r.get("role"))
        watched = ((r.get("alerts_for") or "") + "," +
                   (ri.get("categories") or "")).lower()
        wants_cat = bool(cat) and cat.lower() in watched
        is_lead = bool(ri.get("alert_on_high")) or "lead" in (r.get("role") or "").lower()
        if (high and is_lead) or wants_cat:
            recipients.add(addr)
    for addr in recipients:
        head = "🚨 URGENT" if high else "📣 Heads-up"
        send_any(addr, f"{head} · {cat}\n{TYPE_ICON.get(f.get('entry_type'), '📝')} "
                       f"{summary}\n👉 {ask}", whatsapp)
    if recipients:
        reply += f" 📣 Alerted {len(recipients)} teammate(s)."
    elif high:
        reply += " 🔴 High urgency — you're the on-duty lead on file."
    return reply


def handle_discard(sender):
    pending = get_pending(sender)
    if not pending:
        return "🤷 Nothing waiting to confirm."
    at_update(AT_LOG, pending["id"], {"status": "discarded"})
    return "🗑 Discarded — nothing saved."


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
        "entry_type": structured.get("entry_type") or "observation",
        "follow_up": bool(structured.get("follow_up")),
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
    followups = at_list(
        AT_LOG,
        formula=("AND({status}='confirmed', {follow_up}=TRUE(), "
                 "IS_AFTER({timestamp}, DATEADD(NOW(), -72, 'hours')))"),
        max_records=10, sort_desc_by="timestamp")
    if not recs and not followups:
        return "📋 All quiet — no confirmed entries in the last 24h."

    def etype(f):
        return f.get("entry_type") or "observation"

    fields = [r["fields"] for r in recs]
    done = [f for f in fields if etype(f) == "completion"]
    know = [f for f in fields if etype(f) == "knowledge"]
    high = [f for f in fields
            if f.get("urgency") == "high" and etype(f) not in ("completion", "knowledge")]
    rest = [f for f in fields
            if f not in done and f not in know and f not in high
            and not f.get("follow_up")]

    lines = [f"📋 Shift brief · {len(recs)} new in last 24h"]
    if high:
        lines.append("\n🔴 Urgent:")
        for f in high:
            lines.append(f"• {f.get('summary')}")
            if f.get("the_ask"):
                lines.append(f"   👉 {f.get('the_ask')}")
    if followups:
        lines.append("\n⏰ Check today:")
        lines += [f"• {r['fields'].get('summary')}" for r in followups[:5]]
    if done:
        lines.append("\n✅ Done:")
        lines += [f"• {f.get('summary')}" for f in done[:5]]
    if know:
        lines.append("\n📌 New standing notes:")
        lines += [f"• {f.get('summary')}" for f in know[:5]]
    if rest:
        lines.append("\n🗒 Also logged:")
        lines += [f"• {f.get('summary')} ({f.get('category')})" for f in rest[:8]]
        if len(rest) > 8:
            lines.append(f"   …+{len(rest) - 8} more on the board")
    return "\n".join(lines)

# ---------------------------------------------------------------- F3 lookup

def handle_lookup(query: str):
    words = [w for w in query.lower().split() if len(w) >= 3]
    if not words:
        return "Send: find <keyword>, e.g. find pallet jack"
    recs = at_list(AT_LOG,
                   formula="OR({status}='confirmed', {status}='resolved')",
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
    lines = [f"🔎 Top {min(len(scored), 3)} of {len(scored)} matches:"]
    for _, f in scored[:3]:
        icon = ("✅" if f.get("status") == "resolved"
                else TYPE_ICON.get(f.get("entry_type"), "📝"))
        mark = " — resolved" if f.get("status") == "resolved" else ""
        lines.append(f"{icon} {f.get('summary')}{mark} "
                     f"({(f.get('timestamp') or '')[:10]})")
        if f.get("the_ask") and f.get("status") != "resolved":
            lines.append(f"   👉 {f.get('the_ask')}")
    return "\n".join(lines)

# ---------------------------------------------------------------- dispatch

def dispatch_text(sender: str, body: str, whatsapp: bool = False):
    """Route one inbound text to the right flow. Returns reply text, or None
    for silence. Channel-agnostic: used by the Twilio and Telegram webhooks.
    Exact commands are instant fast-paths; everything else goes through the
    natural-language intent classifier (one Gemini call)."""
    low = body.lower()
    if low.startswith("/start"):
        return "OpsBrain ready. " + USAGE_HINT
    if low in ("yes", "y", "yes."):
        return handle_confirm(sender, whatsapp)
    if low in ("no", "cancel", "discard"):
        return handle_discard(sender)
    if low in ("brief", "breif", "daily brief"):
        return handle_brief()
    for prefix in ("find ", "lookup ", "search "):
        if low.startswith(prefix):
            return handle_lookup(body[len(prefix):])
    if low in ("find", "lookup", "search"):
        return "Send: find <keyword>, e.g. find pallet jack"
    if not body:
        return None
    pending = get_pending(sender)
    try:
        structured = gemini_structure(
            text=body,
            pending_summary=pending["fields"].get("summary") if pending else None)
    except Exception as e:
        log.exception("structuring failed")
        create_entry(sender, body, error_note=f"structuring failed: {e}")
        return ("Couldn't auto-process that, but your note is saved for review "
                "as-is. Reply YES to keep it, or resend.")
    return route_nl(sender, structured, body, whatsapp, pending)


# ---------------------------------------------------------------- webhooks

_last_requests = deque(maxlen=10)


@app.api_route("/", methods=["GET", "POST"])
@app.api_route("/sms", methods=["GET", "POST"])
async def inbound_sms(request: Request):
    # accept params however Twilio is configured to deliver them
    raw_body = await request.body()  # cached; form() below reuses it
    form = dict(request.query_params)
    if request.method == "POST":
        form.update(await request.form())
    _last_requests.append({
        "t": now_iso(), "method": request.method, "url": str(request.url),
        "content_type": request.headers.get("content-type", ""),
        "user_agent": request.headers.get("user-agent", ""),
        "accept": request.headers.get("accept", ""),
        "raw_body": raw_body[:1500].decode(errors="replace"),
    })
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
            return twiml_message(handle_voice(
                sender, media.content, form.get("MediaContentType0"), whatsapp))

        reply = dispatch_text(sender, body, whatsapp)
        return twiml_message(reply) if reply else twiml_empty()
    except Exception:
        log.exception("inbound handling failed")
        return twiml_message("Something went wrong on our side — your message "
                             "was not saved. Please resend in a minute.")


@app.post("/telegram")
async def inbound_telegram(request: Request):
    if not TELEGRAM_TOKEN:
        return {"ok": True}
    expected = hashlib.sha256(TELEGRAM_TOKEN.encode()).hexdigest()[:32]
    if request.headers.get("x-telegram-bot-api-secret-token") != expected:
        return {"ok": True}
    update = await request.json()
    msg = update.get("message") or update.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not chat_id:
        return {"ok": True}
    uid = update.get("update_id")
    if uid is not None and uid in _seen_updates:
        return {"ok": True}          # Telegram retry of an update we have
    _seen_updates.append(uid)
    sender = f"tg:{chat_id}"
    _last_requests.append({
        "t": now_iso(), "telegram_chat_id": chat_id,
        "from_name": (msg.get("from") or {}).get("first_name", ""),
        "text": (msg.get("text") or "[voice/media]")[:40],
    })
    # ack immediately — the LLM round-trip is slower than Telegram's webhook
    # timeout, and a slow response makes Telegram retry (duplicate messages)
    threading.Thread(target=_process_telegram, args=(msg, chat_id, sender),
                     daemon=True).start()
    return {"ok": True}


_seen_updates = deque(maxlen=300)


def _process_telegram(msg, chat_id, sender):
    try:
        if not is_allowed(sender):
            log.info("ignored non-roster telegram chat %s", chat_id)
            return
        requests.post(f"{TG_API}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"}, timeout=10)
        voice = msg.get("voice") or msg.get("audio")
        if voice:
            meta = requests.get(f"{TG_API}/getFile",
                                params={"file_id": voice["file_id"]},
                                timeout=15).json()
            audio = requests.get(
                f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/"
                f"{meta['result']['file_path']}", timeout=30).content
            reply = handle_voice(
                sender, audio, voice.get("mime_type") or "audio/ogg", False)
        else:
            reply = dispatch_text(sender, (msg.get("text") or "").strip())
        if reply:
            send_telegram(chat_id, reply)
    except Exception:
        log.exception("telegram handling failed")
        send_telegram(chat_id, "Something went wrong on our side — your "
                               "message was not saved. Please resend in a minute.")


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
            reply = handle_voice(sender, audio, "audio/mpeg", False)
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


@app.get("/api/state")
def api_state():
    """Everything the Ops Board needs. Never includes sender_phone."""
    recs = at_list(AT_LOG,
                   formula="OR({status}='confirmed', {status}='resolved')",
                   max_records=200, sort_desc_by="timestamp")
    entries = []
    for r in recs:
        f = r["fields"]
        entries.append({
            "id": r["id"],
            "ts": f.get("timestamp", ""),
            "type": f.get("entry_type") or "observation",
            "category": f.get("category", ""),
            "urgency": f.get("urgency", ""),
            "summary": f.get("summary") or f.get("raw_transcript", "")[:120],
            "ask": f.get("the_ask", ""),
            "impact": f.get("stated_impact", ""),
            "status": f.get("status", ""),
            "follow_up": bool(f.get("follow_up")),
            "resolution": f.get("resolution_note", ""),
            "raw": f.get("raw_transcript", "")[:400],
        })
    team = []
    for r in get_roster():
        if not r.get("active"):
            continue
        ri = role_info(r.get("role"))
        team.append({"name": r.get("name") or "—", "role": r.get("role", ""),
                     "alerts": (r.get("alerts_for") or ri.get("categories") or ""),
                     "high": bool(ri.get("alert_on_high"))
                             or "lead" in (r.get("role") or "").lower()})
    roles = [{"role": r.get("role", ""), "categories": r.get("categories", ""),
              "high": bool(r.get("alert_on_high")),
              "desc": r.get("description", "")} for r in get_roles()]
    return {"at": now_iso(), "entries": entries, "team": team, "roles": roles}


@app.get("/dashboard")
def dashboard():
    from dashboard_html import DASH_HTML
    return Response(content=DASH_HTML, media_type="text/html")


@app.get("/debug/last")
def debug_last(key: str = ""):
    if key != "opsbrain-dbg-7391":
        return {"error": "missing key"}
    return list(_last_requests)


# registered last: any other path (trailing slash, typo) still reaches the
# message handler, so a slightly-off webhook URL in the Twilio console works
@app.api_route("/{_path:path}", methods=["GET", "POST"])
async def catch_all(request: Request):
    log.info("webhook hit on non-standard path: %s", request.url.path)
    return await inbound_sms(request)


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
