"""Blocking Govee cloud client (run inside the executor).

Implements the Govee Home app's undocumented auth so we can subscribe to the
account's AWS IoT MQTT stream. Govee enforced email 2FA in mid-2026:
  1. POST /account/rest/account/v2/login  (454 => verification required)
  2. POST /account/rest/account/v1/verification {type:8,email}  => emails a code
  3. re-login including {"code": <code>}
Once a clientId (uuidv5 of the email) has completed a verified login it is
trusted, so subsequent logins from the same client need no code.
"""
from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass

import requests
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    pkcs12,
)

APP_VERSION = "7.4.10"
USER_AGENT = (
    f"GoveeHome/{APP_VERSION} (com.ihoment.GoVeeSensor; build:8; iOS 26.5.0) "
    "Alamofire/5.11.0"
)
LOGIN_URL = "https://app2.govee.com/account/rest/account/v2/login"
VERIFICATION_URL = "https://app2.govee.com/account/rest/account/v1/verification"
IOT_KEY_URL = "https://app2.govee.com/app/v1/account/iot/key"
DEVICE_LIST_URL = "https://app2.govee.com/device/rest/devices/v1/list"
# H5054 (RF sensor via H5040 gateway) leak alerts live in the account's
# warning-message history, not on MQTT. warnLifted is the app's "Read" button.
LEAK_WARN_URL = "https://app2.govee.com/leak/rest/device/v1/warnMessage"
LEAK_WARN_LIFTED_URL = "https://app2.govee.com/leak/rest/device/v1/warnLifted"


class GoveeAuthError(Exception):
    """Authentication failed."""


class NeedsVerificationCode(GoveeAuthError):
    """Govee requires an emailed 2FA verification code."""


@dataclass
class GoveeCreds:
    """Everything needed to open the AWS IoT MQTT connection."""

    token: str
    account_id: int
    topic: str
    endpoint: str
    cert_pem: bytes
    key_pem: bytes
    mqtt_client_id: str


def _client_id(email: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, email).hex


def _ms_timestamp() -> str:
    return str(int(time.time() * 1000))


def _headers(email: str, token: str | None = None) -> dict:
    h = {
        "appVersion": APP_VERSION,
        "clientId": _client_id(email),
        "clientType": "1",
        "iotVersion": "0",
        "timestamp": _ms_timestamp(),
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class GoveeCloud:
    """Blocking Govee cloud API client."""

    def __init__(self, email: str, password: str) -> None:
        self.email = email
        self.password = password

    # -- auth ------------------------------------------------------------- #
    def request_verification_code(self) -> None:
        r = requests.post(
            VERIFICATION_URL,
            headers=_headers(self.email),
            json={"type": 8, "email": self.email},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") not in (200, None):
            raise GoveeAuthError(
                f"Failed to request verification code: {data.get('message', data)}"
            )

    def login(self, code: str | None = None) -> tuple[str, int, str]:
        """Return (token, account_id, account_topic)."""
        body = {
            "email": self.email,
            "password": self.password,
            "client": _client_id(self.email),
        }
        if code:
            body["code"] = code
        r = requests.post(LOGIN_URL, headers=_headers(self.email), json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status == 454:
            if not code:
                raise NeedsVerificationCode(
                    "Govee requires an email verification code."
                )
            raise GoveeAuthError(
                f"Verification code rejected: {data.get('message') or 'invalid/expired'}"
            )
        if status != 200 or "client" not in data:
            raise GoveeAuthError(
                f"Login failed (status {status}): {data.get('message', data)}"
            )
        c = data["client"]
        return c["token"], int(c["accountId"]), c["topic"]

    def get_iot_key(self, token: str) -> tuple[str, str, str]:
        """Return (endpoint, p12_base64, p12_pass)."""
        r = requests.get(IOT_KEY_URL, headers=_headers(self.email, token), timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != 200 or "data" not in data:
            raise GoveeAuthError(f"iot/key failed: {data.get('message', data)}")
        d = data["data"]
        return d["endpoint"], d["p12"], d["p12Pass"]

    def device_list(self, token: str) -> dict:
        r = requests.post(
            DEVICE_LIST_URL, headers=_headers(self.email, token), json={}, timeout=30
        )
        if r.status_code == 401:
            raise GoveeAuthError("device list: token rejected (401)")
        r.raise_for_status()
        return r.json()

    def warn_messages(self, token: str, device: str, sku: str) -> list:
        """Return the leak warning-message history for a polled sensor."""
        r = requests.post(
            LEAK_WARN_URL,
            headers=_headers(self.email, token),
            json={"device": device.replace(":", ""), "limit": 50, "sku": sku},
            timeout=30,
        )
        if r.status_code == 401:
            raise GoveeAuthError("warnMessage: token rejected (401)")
        r.raise_for_status()
        data = r.json()
        if data.get("status") == 401:
            raise GoveeAuthError("warnMessage: token rejected (401)")
        msgs = data.get("data")
        return msgs if isinstance(msgs, list) else []

    def warn_lifted(self, token: str, device: str, sku: str) -> None:
        """Mark all of a polled sensor's leak alerts read (app "Read" button)."""
        r = requests.post(
            LEAK_WARN_LIFTED_URL,
            headers=_headers(self.email, token),
            json={"device": device.replace(":", ""), "sku": sku},
            timeout=30,
        )
        if r.status_code == 401:
            raise GoveeAuthError("warnLifted: token rejected (401)")
        r.raise_for_status()
        data = r.json()
        if data.get("status") not in (200, None):
            raise RuntimeError(f"warnLifted failed: {data.get('message', data)}")

    # -- combined --------------------------------------------------------- #
    def authenticate(self, code: str | None = None) -> GoveeCreds:
        token, account_id, topic = self.login(code)
        endpoint, p12_b64, p12_pass = self.get_iot_key(token)
        cert_pem, key_pem = _p12_to_pem(p12_b64, p12_pass)
        # AWS IoT's policy only authorizes client ids of the form
        # "AP/{accountId}/{random}" -- anything else is dropped (rc=7).
        mqtt_client_id = f"AP/{account_id}/{uuid.uuid4().hex[:16]}"
        return GoveeCreds(
            token=token,
            account_id=account_id,
            topic=topic,
            endpoint=endpoint,
            cert_pem=cert_pem,
            key_pem=key_pem,
            mqtt_client_id=mqtt_client_id,
        )


def _p12_to_pem(p12_b64: str, p12_pass: str) -> tuple[bytes, bytes]:
    raw = base64.b64decode(p12_b64)
    key, cert, _chain = pkcs12.load_key_and_certificates(raw, p12_pass.encode("utf-8"))
    if cert is None or key is None:
        raise GoveeAuthError("p12 did not contain both a cert and a key")
    return (
        cert.public_bytes(Encoding.PEM),
        key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()),
    )
