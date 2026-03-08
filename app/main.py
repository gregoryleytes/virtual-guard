"""
Virtual Guard - AI-powered HOA Gate Assistant
Powered by Claude (Anthropic) + Twilio + IZCloud
Inex Technology — v2.0
"""

import os
import json
import logging
import re
from datetime import datetime
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather, Dial
from twilio.twiml.messaging_response import MessagingResponse

from .izcloud import IZCloudClient
from .conversation import ConversationManager
from .state import SessionStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("virtual_guard")

app = Flask(__name__)

# ── Clients ──────────────────────────────────────────────────────────────────
iz = IZCloudClient(
    base_url=os.environ["IZCLOUD_BASE_URL"],
    username=os.environ["IZCLOUD_USERNAME"],
    password=os.environ["IZCLOUD_PASSWORD"],
)
sessions = SessionStore()
mgr = ConversationManager(iz_client=iz, session_store=sessions)

MANAGER_PHONE   = os.environ.get("MANAGER_PHONE", "+12016382317")
TWILIO_NUMBER   = os.environ.get("TWILIO_NUMBER",  "+12202228122")
HOA_NAME        = os.environ.get("HOA_NAME",        "your community")


# ═══════════════════════════════════════════════════════════════════════════════
#  VOICE — INTERCOM / VISITOR FLOW
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/voice/incoming", methods=["POST"])
def voice_incoming():
    """
    Entry point: Twilio calls this when intercom places a call.
    Greets visitor and asks for PIN.
    """
    call_sid = request.form.get("CallSid", "unknown")
    caller   = request.form.get("From", "")
    logger.info("VOICE INCOMING  call_sid=%s from=%s", call_sid, caller)

    # Initialise session
    sessions.init(call_sid, {"type": "voice", "caller": caller, "attempts": 0})

    resp = VoiceResponse()
    gather = Gather(
        input="dtmf speech",
        action="/voice/pin_received",
        method="POST",
        timeout=10,
        num_digits=6,
        speech_timeout="auto",
        language="en-US",
    )
    gather.say(
        f"Welcome to {HOA_NAME}. "
        "Please enter or say your access PIN now, or press star to speak with the manager.",
        voice="Polly.Joanna",
    )
    resp.append(gather)
    # No input fallback
    resp.redirect("/voice/no_input")
    return Response(str(resp), mimetype="text/xml")


@app.route("/voice/pin_received", methods=["POST"])
def voice_pin_received():
    call_sid   = request.form.get("CallSid", "unknown")
    digits     = request.form.get("Digits", "").strip()
    speech     = request.form.get("SpeechResult", "").strip()
    session    = sessions.get(call_sid) or {}
    attempts   = session.get("attempts", 0)

    logger.info("PIN_RECEIVED call_sid=%s digits=%r speech=%r", call_sid, digits, speech)

    resp = VoiceResponse()

    # Star key → transfer to manager
    if digits == "*":
        return _transfer_to_manager(resp, call_sid, reason="requested")

    # Extract PIN from digits or speech
    pin = digits if digits else _extract_pin_from_speech(speech)

    if not pin:
        return _retry_or_transfer(resp, call_sid, attempts, "I didn't catch a PIN.")

    # Validate PIN with IZCloud
    result = iz.validate_pin(pin)
    if result.get("valid"):
        gate_uid = result.get("gate_uid") or iz.get_first_gate_uid()
        iz.open_gate(gate_uid)
        logger.info("GATE OPENED  call_sid=%s pin=%s gate=%s", call_sid, _mask(pin), gate_uid)
        sessions.clear(call_sid)
        resp.say(
            "Access granted. Welcome! The gate is opening now.",
            voice="Polly.Joanna",
        )
        resp.hangup()
    else:
        attempts += 1
        sessions.update(call_sid, {"attempts": attempts})
        if attempts >= 3:
            return _transfer_to_manager(resp, call_sid, reason="too_many_attempts")
        gather = Gather(
            input="dtmf speech",
            action="/voice/pin_received",
            method="POST",
            timeout=10,
            num_digits=6,
            speech_timeout="auto",
        )
        gather.say(
            f"That PIN was not recognised. Please try again. Attempt {attempts + 1} of 3.",
            voice="Polly.Joanna",
        )
        resp.append(gather)
        resp.redirect("/voice/no_input")

    return Response(str(resp), mimetype="text/xml")


@app.route("/voice/no_input", methods=["POST"])
def voice_no_input():
    call_sid = request.form.get("CallSid", "unknown")
    resp = VoiceResponse()
    resp.say("We didn't receive a response. Transferring you to the manager.", voice="Polly.Joanna")
    return _transfer_to_manager(resp, call_sid, reason="no_input")


@app.route("/voice/manager_status", methods=["POST"])
def voice_manager_status():
    """Callback after manager call attempt."""
    call_sid      = request.form.get("CallSid", "unknown")
    dial_status   = request.form.get("DialCallStatus", "")
    logger.info("MANAGER STATUS call_sid=%s status=%s", call_sid, dial_status)

    resp = VoiceResponse()
    if dial_status in ("busy", "no-answer", "failed", "canceled"):
        resp.say(
            "The manager is unavailable. Please try again later or use the resident SMS line. Goodbye.",
            voice="Polly.Joanna",
        )
        resp.hangup()
    return Response(str(resp), mimetype="text/xml")


# ═══════════════════════════════════════════════════════════════════════════════
#  SMS — RESIDENT FLOW  (Claude AI handles conversation)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/sms/incoming", methods=["POST"])
def sms_incoming():
    from_number = request.form.get("From", "")
    body        = request.form.get("Body", "").strip()
    msg_sid     = request.form.get("MessageSid", "unknown")

    logger.info("SMS INCOMING  msg_sid=%s from=%s body=%r", msg_sid, from_number, body[:80])

    # Normalise phone
    phone = _normalise_phone(from_number)

    # Get or init session
    session_key = f"sms_{phone}"
    session     = sessions.get(session_key) or {}

    # Verify resident is registered before handing off to AI
    if not session.get("resident"):
        resident = iz.find_resident_by_phone(phone)
        if not resident:
            resp = MessagingResponse()
            hoa = os.environ.get("HOA_NAME", "this community")
            resp.message(
                f"Sorry, your number is not registered at {hoa}. "
                "Please contact your property manager."
            )
            return Response(str(resp), mimetype="text/xml")
        session["resident"] = resident

    # Route to conversation manager (Claude)
    reply_text, updated_session = mgr.handle_sms(
        phone=phone,
        message=body,
        session=session,
    )

    sessions.set(session_key, updated_session)

    resp = MessagingResponse()
    resp.message(reply_text)

    logger.info("SMS REPLY  msg_sid=%s to=%s reply=%r", msg_sid, phone, reply_text[:80])
    return Response(str(resp), mimetype="text/xml")


# ═══════════════════════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat(), "service": "virtual-guard-v2"}


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _transfer_to_manager(resp: VoiceResponse, call_sid: str, reason: str) -> Response:
    logger.info("TRANSFER_TO_MANAGER call_sid=%s reason=%s", call_sid, reason)
    sessions.clear(call_sid)
    resp.say("Please hold while I connect you with the property manager.", voice="Polly.Joanna")
    dial = Dial(action="/voice/manager_status", method="POST", timeout=30)
    dial.number(MANAGER_PHONE)
    resp.append(dial)
    return Response(str(resp), mimetype="text/xml")


def _retry_or_transfer(resp: VoiceResponse, call_sid: str, attempts: int, msg: str) -> Response:
    if attempts >= 2:
        resp.say(msg + " Transferring to manager.", voice="Polly.Joanna")
        return _transfer_to_manager(resp, call_sid, reason="max_retries")
    gather = Gather(
        input="dtmf speech",
        action="/voice/pin_received",
        method="POST",
        timeout=10,
        num_digits=6,
        speech_timeout="auto",
    )
    gather.say(msg + " Please try again.", voice="Polly.Joanna")
    resp.append(gather)
    resp.redirect("/voice/no_input")
    return Response(str(resp), mimetype="text/xml")


def _extract_pin_from_speech(text: str) -> str:
    """Extract digit sequence from speech recognition result."""
    digits = re.sub(r"\D", "", text)
    return digits if 4 <= len(digits) <= 8 else ""


def _normalise_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _mask(pin: str) -> str:
    return "*" * max(0, len(pin) - 2) + pin[-2:] if pin else ""
