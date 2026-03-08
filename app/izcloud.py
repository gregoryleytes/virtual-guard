"""
IZCloud REST API client — paths verified against Swagger /inexapi/...
Base: https://locks2.iz-cloud.com:46443
Auth: POST /inexapi/chatBot/Auth  →  Bearer token
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("virtual_guard.izcloud")


class IZCloudError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class IZCloudClient:
    TOKEN_MARGIN = 60

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 10):
        self.base_url  = base_url.rstrip("/")
        self.username  = username  # client_id
        self.password  = password  # client_secret
        self.timeout   = timeout
        self._token: Optional[str] = None
        self._token_expires: float = 0.0

        self._session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.verify = False

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - self.TOKEN_MARGIN:
            return self._token
        logger.info("Refreshing IZCloud token")
        resp = self._session.post(
            f"{self.base_url}/inexapi/chatBot/Auth",
            json={"client_id": self.username, "client_secret": self.password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = (data.get("token") or data.get("access_token")
                       or data.get("accessToken") or data.get("Bearer"))
        expires_in = data.get("expires_in") or data.get("expiresIn") or 3600
        self._token_expires = time.time() + int(expires_in)
        logger.info("IZCloud token refreshed, expires in %ds", expires_in)
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    def _get(self, path: str, params: Dict = None) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        logger.info("IZCloud GET %s -> status=%d body=%r", path, r.status_code, r.text[:800])
        if not r.ok:
            raise IZCloudError(f"GET {path} failed: {r.status_code} {r.text}", r.status_code)
        text = r.text.strip()
        if not text:
            return {}
        try:
            return r.json()
        except Exception as e:
            logger.warning("GET %s non-JSON response: %s | body=%r", path, e, r.text[:300])
            return {}

    def _post(self, path: str, body: Dict) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.post(url, headers=self._headers(), json=body, timeout=self.timeout)
        logger.info("IZCloud POST %s -> status=%d body=%r", path, r.status_code, r.text[:800])
        if not r.ok:
            raise IZCloudError(f"POST {path} failed: {r.status_code} {r.text}", r.status_code)
        text = r.text.strip()
        if not text:
            return {}
        try:
            return r.json()
        except Exception as e:
            logger.warning("POST %s non-JSON response: %s | body=%r", path, e, r.text[:300])
            return {}

    def _delete(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.delete(url, headers=self._headers(), timeout=self.timeout)
        logger.debug("DELETE %s -> %d", url, r.status_code)
        if not r.ok:
            raise IZCloudError(f"DELETE {path} failed: {r.status_code} {r.text}", r.status_code)
        return r.json() if r.content else {}

    # ── Resident lookup ───────────────────────────────────────────────────────

    def find_resident_by_phone(self, phone: str) -> Optional[Dict]:
        """POST /inexapi/person/find"""
        try:
            data = self._post("/inexapi/person/find", {
                "phone_number": phone,
                "type": "Resident"
            })
            persons = data.get("person", [])
            return persons[0] if persons else None
        except IZCloudError as e:
            logger.warning("Resident lookup failed for %s: %s", phone, e)
            return None

    def get_resident(self, resident_id: int) -> Optional[Dict]:
        """GET /inexapi/person/id/{id}"""
        try:
            return self._get(f"/inexapi/person/id/{resident_id}")
        except IZCloudError:
            return None

    # ── PIN validation ────────────────────────────────────────────────────────

    def validate_pin(self, pin: str) -> Dict:
        """Check all guest-pins for a match. No dedicated validate endpoint exists."""
        try:
            data = self._get("/inexapi/guest-pin")
            pins = data.get("pins", [])
            for p in pins:
                if p.get("pin") == pin and not p.get("is_expired", True):
                    logger.info("PIN matched guest pin id=%s", p.get("id"))
                    return {"valid": True, "gate_uid": None, "pin_data": p}
            return {"valid": False}
        except IZCloudError as e:
            logger.warning("PIN validation error: %s", e)
            return {"valid": False}

    # ── Gate open ─────────────────────────────────────────────────────────────

    def open_gate(self, gate_uid: str, resident_id: int = None) -> bool:
        """POST /inexapi/gate/person/id/{id}/gate/open/{gate_uid}"""
        try:
            if resident_id:
                path = f"/inexapi/gate/person/id/{resident_id}/gate/open/{gate_uid}"
            else:
                path = f"/inexapi/gate/person/uid/visitor/gate/open/{gate_uid}"
            self._post(path, {"visitor_info": {"name": "Visitor", "reason": "Gate access"}})
            logger.info("Gate %s opened", gate_uid)
            return True
        except IZCloudError as e:
            logger.error("Failed to open gate %s: %s", gate_uid, e)
            return False

    # ── Gates ─────────────────────────────────────────────────────────────────

    def list_gates(self) -> List[Dict]:
        """GET /inexapi/gate"""
        try:
            data = self._get("/inexapi/gate")
            return data.get("gates", [])
        except IZCloudError as e:
            logger.error("list_gates failed: %s", e)
            return []

    def get_first_gate_uid(self) -> Optional[str]:
        gates = self.list_gates()
        return gates[0].get("uid") if gates else None

    # ── Guest PINs ────────────────────────────────────────────────────────────

    def list_guest_pins(self, resident_id: int) -> List[Dict]:
        """GET /inexapi/person/id/{id}/pin"""
        try:
            data = self._get(f"/inexapi/person/id/{resident_id}/pin")
            return data.get("pins", [])
        except IZCloudError as e:
            logger.error("list_guest_pins failed: %s", e)
            return []

    def create_guest_pin(self, resident_id: int, label: str,
                         valid_from: str = None, valid_to: str = None,
                         gate_ids: List[str] = None) -> Dict:
        """POST /inexapi/person/id/{id}/guest-pin"""
        body: Dict[str, Any] = {
            "name":           label,
            "notify_via_sms": False,
            "duration":       {"value": 1, "unit": "Days"},
        }
        if valid_from or valid_to:
            body["access"] = {
                "limited_interval": {
                    "datetime_from": valid_from,
                    "datetime_to":   valid_to,
                }
            }
        return self._post(f"/inexapi/person/id/{resident_id}/guest-pin", body)

    def delete_guest_pin(self, pin_id: int) -> bool:
        """DELETE /inexapi/guest-pin/{id}"""
        try:
            self._delete(f"/inexapi/guest-pin/{pin_id}")
            return True
        except IZCloudError as e:
            logger.error("delete_guest_pin %s failed: %s", pin_id, e)
            return False

    def regenerate_guest_pin(self, pin_id: int) -> Dict:
        return {"error": "Regenerate not supported directly. Delete and create a new PIN."}

    # ── Vehicles ──────────────────────────────────────────────────────────────

    def list_vehicles(self, resident_id: int) -> List[Dict]:
        """GET /inexapi/person/id/{id}/vehicle"""
        try:
            data = self._get(f"/inexapi/person/id/{resident_id}/vehicle")
            return data.get("vehicles", [])
        except IZCloudError as e:
            logger.error("list_vehicles failed: %s", e)
            return []

    def add_vehicle(self, resident_id: int, plate: str,
                    make: str = "", model: str = "", color: str = "") -> Dict:
        """POST /inexapi/person/id/{id}/vehicle"""
        body = {
            "license_plate": plate.upper(),
            "make":  make,
            "model": model,
            "color": color,
            "is_guest": False,
        }
        return self._post(f"/inexapi/person/id/{resident_id}/vehicle", body)

    def remove_vehicle(self, resident_id: int, vehicle_id: int) -> bool:
        """DELETE /inexapi/person/vehicle/{id}"""
        try:
            self._delete(f"/inexapi/person/vehicle/{vehicle_id}")
            return True
        except IZCloudError as e:
            logger.error("remove_vehicle failed: %s", e)
            return False

    # ── Events ────────────────────────────────────────────────────────────────

    def log_event(self, event_type: str, data: Dict) -> None:
        logger.info("EVENT %s: %s", event_type, data)
