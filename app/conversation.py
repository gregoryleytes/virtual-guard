"""
ConversationManager — handles resident SMS conversations using Claude (Anthropic).

Flow:
  1. Incoming SMS arrives with phone number and message text.
  2. We look up the resident in IZCloud by phone.
  3. We build a Claude conversation with system prompt + history.
  4. Claude responds, possibly invoking tool calls to IZCloud.
  5. We execute the tool calls and return the final text reply.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from .izcloud import IZCloudClient, IZCloudError
from .state import SessionStore

logger = logging.getLogger("virtual_guard.conversation")

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1024

HOA_NAME  = os.environ.get("HOA_NAME", "your community")


# ── Tool definitions passed to Claude ────────────────────────────────────────

TOOLS: List[Dict] = [
    {
        "name": "open_gate",
        "description": "Open a specific gate for a verified resident.",
        "input_schema": {
            "type": "object",
            "properties": {
                "gate_id": {"type": "string", "description": "The IZCloud gate ID to open."},
            },
            "required": ["gate_id"],
        },
    },
    {
        "name": "list_guest_pins",
        "description": "List all active guest PINs for the resident.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "create_guest_pin",
        "description": "Create a new guest PIN for a visitor. Returns the PIN number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label":      {"type": "string",  "description": "Descriptive label, e.g. 'Plumber Thursday'."},
                "valid_from": {"type": "string",  "description": "ISO-8601 start datetime (optional)."},
                "valid_to":   {"type": "string",  "description": "ISO-8601 end datetime (optional)."},
                "gate_ids":   {"type": "array",   "items": {"type": "string"},
                               "description": "Gate IDs the PIN will work on (optional, defaults to all)."},
            },
            "required": ["label"],
        },
    },
    {
        "name": "delete_guest_pin",
        "description": "Delete (revoke) a guest PIN by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pin_id": {"type": "string", "description": "The guest PIN record ID to delete."},
            },
            "required": ["pin_id"],
        },
    },
    {
        "name": "regenerate_guest_pin",
        "description": "Regenerate (change) a guest PIN to a new random value.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pin_id": {"type": "string", "description": "The guest PIN record ID to regenerate."},
            },
            "required": ["pin_id"],
        },
    },
    {
        "name": "list_vehicles",
        "description": "List the vehicles registered to this resident.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "add_vehicle",
        "description": "Register a new vehicle for the resident.",
        "input_schema": {
            "type": "object",
            "properties": {
                "plate": {"type": "string", "description": "License plate number."},
                "make":  {"type": "string", "description": "Vehicle make (e.g. Toyota)."},
                "model": {"type": "string", "description": "Vehicle model (e.g. Camry)."},
                "color": {"type": "string", "description": "Vehicle color."},
            },
            "required": ["plate"],
        },
    },
    {
        "name": "remove_vehicle",
        "description": "Remove a vehicle from the resident's account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vehicle_id": {"type": "string", "description": "The vehicle record ID to remove."},
            },
            "required": ["vehicle_id"],
        },
    },
    {
        "name": "list_gates",
        "description": "List available gates the resident can access.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


SYSTEM_PROMPT = """You are the Virtual Gate Assistant for {hoa_name}.
You communicate via SMS and help VERIFIED RESIDENTS manage their gate access.

Current resident: {resident_name} (Unit {unit})

Your capabilities:
• Open gates remotely for the resident
• Create, list, and delete guest PINs for visitors
• Regenerate guest PINs that have been compromised or expired
• List and update registered vehicles
• Answer questions about access and gate hours

Rules:
1. You are talking to a verified, authenticated resident. Never ask them to verify their identity again in this session.
2. NEVER read out a PIN number in plaintext in a voice context. In SMS, PINs are acceptable.
3. For guest PINs: always confirm the label/purpose and optionally date/time window before creating.
4. For vehicle changes: always confirm plate and details before writing.
5. Be concise — this is SMS. Keep replies under 160 characters when possible, split to multiple messages if needed.
6. If asked about something outside your scope, politely say to contact the property manager.
7. Log all gate open actions and PIN changes with reason.
8. If unsure about a request, ask a clarifying question before acting.
"""


class ConversationManager:

    def __init__(self, iz_client: IZCloudClient, session_store: SessionStore):
        self.iz       = iz_client
        self.sessions = session_store
        self.client   = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def handle_sms(self, phone: str, message: str,
                   session: Dict) -> Tuple[str, Dict]:
        """
        Process one inbound SMS message from a resident.

        Returns
        -------
        (reply_text, updated_session)
        """
        # 1. Identify resident
        resident = session.get("resident")
        if not resident:
            try:
                resident = self.iz.find_resident_by_phone(phone)
            except Exception:
                resident = None
            if not resident:
                return (
                    f"Sorry, your number is not registered at {HOA_NAME}. "
                    "Please contact your property manager.",
                    session,
                )
            session["resident"] = resident
            logger.info("Resident identified: %s id=%s phone=%s",
                        resident.get("name"), resident.get("id"), phone)

        resident_id   = resident["id"]
        resident_name = resident.get("name", "Resident")
        unit          = resident.get("unit", "N/A")

        # 2. Build message history
        history: List[Dict] = session.get("history", [])
        history.append({"role": "user", "content": message})

        # 3. Call Claude with tool support
        system = SYSTEM_PROMPT.format(
            hoa_name=HOA_NAME,
            resident_name=resident_name,
            unit=unit,
        )

        reply, updated_history = self._run_claude(
            system=system,
            history=history,
            resident_id=resident_id,
        )

        session["history"] = updated_history[-20:]  # keep last 10 turns
        return reply, session

    # ── Claude agentic loop ───────────────────────────────────────────────────

    def _run_claude(self, system: str, history: List[Dict],
                    resident_id: str) -> Tuple[str, List[Dict]]:
        messages = list(history)

        for _ in range(8):  # max 8 tool call rounds to prevent runaway
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            logger.debug("Claude stop_reason=%s", response.stop_reason)

            # Collect text and tool use from response
            assistant_content = response.content
            messages.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                # Final reply — extract text
                text = " ".join(
                    block.text for block in assistant_content
                    if hasattr(block, "text")
                ).strip()
                return text, messages

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in assistant_content:
                    if block.type == "tool_use":
                        result = self._execute_tool(block.name, block.input, resident_id)
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     json.dumps(result),
                        })
                messages.append({"role": "user", "content": tool_results})
                continue

            break

        return "I'm sorry, I couldn't complete that request. Please try again.", messages

    # ── Tool execution ────────────────────────────────────────────────────────

    def _execute_tool(self, name: str, inputs: Dict, resident_id: str) -> Any:
        logger.info("TOOL CALL  %s  inputs=%s  resident=%s", name, inputs, resident_id)

        try:
            if name == "open_gate":
                ok = self.iz.open_gate(inputs["gate_id"])
                return {"success": ok}

            if name == "list_guest_pins":
                pins = self.iz.list_guest_pins(resident_id)
                # Sanitise: return id, label, pin, validFrom, validTo
                return [
                    {k: p.get(k) for k in ("id", "label", "pin", "validFrom", "validTo")}
                    for p in pins
                ]

            if name == "create_guest_pin":
                result = self.iz.create_guest_pin(
                    resident_id=resident_id,
                    label=inputs["label"],
                    valid_from=inputs.get("valid_from"),
                    valid_to=inputs.get("valid_to"),
                    gate_ids=inputs.get("gate_ids"),
                )
                return result  # includes the pin number

            if name == "delete_guest_pin":
                ok = self.iz.delete_guest_pin(inputs["pin_id"])
                return {"success": ok}

            if name == "regenerate_guest_pin":
                result = self.iz.regenerate_guest_pin(inputs["pin_id"])
                return result

            if name == "list_vehicles":
                return self.iz.list_vehicles(resident_id)

            if name == "add_vehicle":
                result = self.iz.add_vehicle(
                    resident_id=resident_id,
                    plate=inputs["plate"],
                    make=inputs.get("make", ""),
                    model=inputs.get("model", ""),
                    color=inputs.get("color", ""),
                )
                return result

            if name == "remove_vehicle":
                ok = self.iz.remove_vehicle(resident_id, inputs["vehicle_id"])
                return {"success": ok}

            if name == "list_gates":
                return self.iz.list_gates()

            return {"error": f"Unknown tool: {name}"}

        except IZCloudError as e:
            logger.error("Tool %s failed: %s", name, e)
            return {"error": str(e), "status_code": e.status_code}
        except Exception as e:
            logger.error("Tool %s unexpected error: %s", name, e)
            return {"error": str(e)}
