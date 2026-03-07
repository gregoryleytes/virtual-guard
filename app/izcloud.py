"""
IZCloud REST API client.
Wraps authentication and all endpoints used by Virtual Guard.

API base: https://locks2.iz-cloud.com:46443
Auth:     Basic (username / password) → returns JWT bearer token
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
    """
    Thread-safe IZCloud client with automatic token refresh.

    Parameters
    ----------
    base_url  : e.g. "https://locks2.iz-cloud.com:46443"
    username  : API username  (env: IZCLOUD_USERNAME)
    password  : API password  (env: IZCLOUD_PASSWORD)
    timeout   : HTTP timeout in seconds (default 10)
    """

    TOKEN_MARGIN = 60  # refresh token 60 s before expiry

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 10):
        self.base_url  = base_url.rstrip("/")
        self.username  = username
        self.password  = password
        self.timeout   = timeout
        self._token: Optional[str] = None
        self._token_expires: float = 0.0

        # Session with retry
        self._session = requests.Session()
        retry = Retry(total=3, backoff_factor=0.5,
                      status_forcelist=[500, 502, 503, 504])
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._session.verify = False  # IZCloud uses self-signed cert — disable in prod if cert installed

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires - self.TOKEN_MARGIN:
            return self._token

        logger.info("Refreshing IZCloud token for user %s", self.username)
        resp = self._session.post(
            f"{self.base_url}/api/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token          = data["token"]          # adjust key to match actual IZCloud response
        expires_in           = data.get("expiresIn", 3600)
        self._token_expires  = time.time() + expires_in
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
        logger.debug("GET %s → %d", url, r.status_code)
        if not r.ok:
            raise IZCloudError(f"GET {path} failed: {r.status_code} {r.text}", r.status_code)
        return r.json()

    def _post(self, path: str, body: Dict) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.post(url, headers=self._headers(), json=body, timeout=self.timeout)
        logger.debug("POST %s → %d", url, r.status_code)
        if not r.ok:
            raise IZCloudError(f"POST {path} failed: {r.status_code} {r.text}", r.status_code)
        return r.json() if r.content else {}

    def _put(self, path: str, body: Dict) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.put(url, headers=self._headers(), json=body, timeout=self.timeout)
        logger.debug("PUT %s → %d", url, r.status_code)
        if not r.ok:
            raise IZCloudError(f"PUT {path} failed: {r.status_code} {r.text}", r.status_code)
        return r.json() if r.content else {}

    def _delete(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        r = self._session.delete(url, headers=self._headers(), timeout=self.timeout)
        logger.debug("DELETE %s → %d", url, r.status_code)
        if not r.ok:
            raise IZCloudError(f"DELETE {path} failed: {r.status_code} {r.text}", r.status_code)
        return r.json() if r.content else {}

    # ── PIN / Access ──────────────────────────────────────────────────────────

    def validate_pin(self, pin: str) -> Dict:
        """
        Validate a visitor PIN.
        Returns {"valid": bool, "gate_id": str, "resident_id": str, ...}
        """
        try:
            data = self._get("/api/access/validatepin", params={"pin": pin})
            logger.info("PIN validated: valid=%s gate=%s", data.get("valid"), data.get("gate_id"))
            return data
        except IZCloudError as e:
            logger.warning("PIN validation error: %s", e)
            return {"valid": False}

    def open_gate(self, gate_id: str) -> bool:
        """Trigger gate open via ACP relay."""
        try:
            self._post(f"/api/access/gates/{gate_id}/open", {})
            logger.info("Gate %s opened", gate_id)
            return True
        except IZCloudError as e:
            logger.error("Failed to open gate %s: %s", gate_id, e)
            return False

    def get_access_list(self, gate_id: str = None) -> List[Dict]:
        """Retrieve access list (all residents / credentials)."""
        params = {}
        if gate_id:
            params["gateId"] = gate_id
        return self._get("/api/access/list", params=params)

    # ── Resident lookup ───────────────────────────────────────────────────────

    def find_resident_by_phone(self, phone: str) -> Optional[Dict]:
        """
        Lookup resident record by phone number.
        Returns resident dict or None.
        """
        try:
            residents = self._get("/api/residents", params={"phone": phone})
            if isinstance(residents, list) and residents:
                return residents[0]
            if isinstance(residents, dict) and residents.get("id"):
                return residents
            return None
        except IZCloudError as e:
            logger.warning("Resident lookup failed for %s: %s", phone, e)
            return None

    def get_resident(self, resident_id: str) -> Optional[Dict]:
        try:
            return self._get(f"/api/residents/{resident_id}")
        except IZCloudError:
            return None

    # ── Guest PINs ────────────────────────────────────────────────────────────

    def create_guest_pin(self, resident_id: str, label: str,
                         valid_from: str = None, valid_to: str = None,
                         gate_ids: List[str] = None) -> Dict:
        """
        Create a guest PIN for a resident.
        valid_from / valid_to : ISO-8601 datetime strings (optional)
        gate_ids              : list of gate IDs the PIN will work on (optional, defaults to all)
        Returns {"pin": "123456", "id": "...", ...}
        """
        body: Dict[str, Any] = {
            "residentId": resident_id,
            "label":       label,
        }
        if valid_from:
            body["validFrom"] = valid_from
        if valid_to:
            body["validTo"] = valid_to
        if gate_ids:
            body["gateIds"] = gate_ids
        return self._post("/api/access/guestpins", body)

    def list_guest_pins(self, resident_id: str) -> List[Dict]:
        return self._get("/api/access/guestpins", params={"residentId": resident_id})

    def delete_guest_pin(self, pin_id: str) -> bool:
        try:
            self._delete(f"/api/access/guestpins/{pin_id}")
            return True
        except IZCloudError as e:
            logger.error("Delete guest PIN %s failed: %s", pin_id, e)
            return False

    def regenerate_guest_pin(self, pin_id: str) -> Dict:
        return self._post(f"/api/access/guestpins/{pin_id}/regenerate", {})

    # ── Vehicles ──────────────────────────────────────────────────────────────

    def list_vehicles(self, resident_id: str) -> List[Dict]:
        return self._get("/api/residents/{}/vehicles".format(resident_id))

    def add_vehicle(self, resident_id: str, plate: str,
                    make: str = "", model: str = "", color: str = "") -> Dict:
        body = {
            "residentId": resident_id,
            "plate":       plate.upper(),
            "make":        make,
            "model":       model,
            "color":       color,
        }
        return self._post(f"/api/residents/{resident_id}/vehicles", body)

    def remove_vehicle(self, resident_id: str, vehicle_id: str) -> bool:
        try:
            self._delete(f"/api/residents/{resident_id}/vehicles/{vehicle_id}")
            return True
        except IZCloudError as e:
            logger.error("Remove vehicle failed: %s", e)
            return False

    # ── Gates ─────────────────────────────────────────────────────────────────

    def list_gates(self) -> List[Dict]:
        return self._get("/api/access/gates")

    # ── Events / Audit ────────────────────────────────────────────────────────

    def log_event(self, event_type: str, data: Dict) -> None:
        try:
            self._post("/api/events", {"type": event_type, "data": data,
                                       "timestamp": _now_iso()})
        except Exception as e:
            logger.warning("Event log failed: %s", e)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
