"""One-time interactive OAuth2 + PKCE setup for the Viessmann Developer API, run via
`piloading auth-viessmann` on a machine with a browser (the Mac, not the Pi).

Prerequisite: a client must already be registered on the Viessmann Developer Portal
(https://developer.viessmann-climatesolutions.com) with `http://localhost:4200/callback`
as an allowed redirect URI, and its client_id set as PILOADING_VIESSMANN_CLIENT_ID.

Deploying to the Pi = copying the resulting token file over; the Pi never runs this.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

import httpx

from piloading.config import Settings

_AUTHORIZE_URL = "https://iam.viessmann-climatesolutions.com/idp/v3/authorize"
_TOKEN_URL = "https://iam.viessmann-climatesolutions.com/idp/v3/token"
_API_BASE = "https://api.viessmann-climatesolutions.com"
_CALLBACK_PORT = 4200
_CALLBACK_TIMEOUT_S = 300.0


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _build_authorize_url(client_id: str, redirect_uri: str, code_challenge: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": "IoT User offline_access",
        "state": state,
    }
    return f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib-mandated method name)
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_error = params.get("error", [None])[0]  # type: ignore[attr-defined]
        self.server.returned_state = params.get("state", [None])[0]  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "Anmeldung erfolgreich, du kannst dieses Fenster schließen."
            if self.server.auth_code  # type: ignore[attr-defined]
            else f"Fehler: {self.server.auth_error}"  # type: ignore[attr-defined]
        )
        self.wfile.write(f"<html><body><h1>{message}</h1></body></html>".encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress default request logging to stderr


def _wait_for_callback(port: int, expected_state: str) -> str:
    server = HTTPServer(("localhost", port), _CallbackHandler)
    server.timeout = _CALLBACK_TIMEOUT_S
    server.auth_code = None  # type: ignore[attr-defined]
    server.auth_error = None  # type: ignore[attr-defined]
    server.returned_state = None  # type: ignore[attr-defined]
    server.handle_request()

    if server.auth_code is None:  # type: ignore[attr-defined]
        detail = server.auth_error or "Zeitüberschreitung beim Warten auf die Anmeldung"  # type: ignore[attr-defined]
        raise RuntimeError(f"Viessmann-Anmeldung fehlgeschlagen: {detail}")
    if server.returned_state != expected_state:  # type: ignore[attr-defined]
        raise RuntimeError("State-Parameter stimmt nicht überein, breche sicherheitshalber ab.")
    return server.auth_code  # type: ignore[attr-defined]


def _exchange_code_for_tokens(client_id: str, redirect_uri: str, code: str, code_verifier: str) -> dict[str, Any]:
    response = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "code": code,
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return response.json()


def _get_json(url: str, token: str) -> Any:
    response = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15.0)
    response.raise_for_status()
    return response.json()


def _pick(items: list[dict[str, Any]], label: str, describe: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    if not items:
        raise RuntimeError(f"Keine {label} gefunden.")
    if len(items) == 1:
        return items[0]
    print(f"\nMehrere {label} gefunden:")
    for i, item in enumerate(items):
        print(f"  [{i}] {describe(item)}")
    choice = input(f"Welche(s) {label} verwenden? [0-{len(items) - 1}]: ").strip()
    return items[int(choice)]


def _discover_device(access_token: str) -> tuple[str, str, str]:
    installations = _get_json(f"{_API_BASE}/iot/v2/equipment/installations", access_token).get("data", [])
    installation = _pick(installations, "Installationen", lambda i: f"id={i.get('id')} {i.get('description', '')}")
    installation_id = str(installation["id"])

    gateways = _get_json(
        f"{_API_BASE}/iot/v2/equipment/installations/{installation_id}/gateways", access_token
    ).get("data", [])
    gateway = _pick(gateways, "Gateways", lambda g: f"serial={g.get('serial')} {g.get('description', '')}")
    gateway_serial = str(gateway["serial"])

    devices = _get_json(
        f"{_API_BASE}/iot/v2/equipment/installations/{installation_id}/gateways/{gateway_serial}/devices",
        access_token,
    ).get("data", [])
    device = _pick(devices, "Geräte", lambda d: f"id={d.get('id')} {d.get('deviceType', '')} {d.get('modelId', '')}")
    device_id = str(device["id"])

    return installation_id, gateway_serial, device_id


def run_auth_setup(settings: Settings) -> None:
    if not settings.viessmann_client_id:
        raise SystemExit("PILOADING_VIESSMANN_CLIENT_ID ist nicht gesetzt (z.B. in .env eintragen).")

    redirect_uri = f"http://localhost:{_CALLBACK_PORT}/callback"
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)
    url = _build_authorize_url(settings.viessmann_client_id, redirect_uri, challenge, state)

    print(f"Voraussetzung: '{redirect_uri}' muss im Viessmann Developer Portal als Redirect-URI")
    print("für diesen Client hinterlegt sein.\n")
    print("Öffne die folgende URL in deinem Browser und melde dich mit deinem Viessmann-Account an:\n")
    print(url)
    print(f"\nWarte auf Weiterleitung nach {redirect_uri} (bis zu {int(_CALLBACK_TIMEOUT_S)}s) ...")

    code = _wait_for_callback(_CALLBACK_PORT, state)

    print("Anmeldung erfolgreich, tausche Code gegen Token ...")
    tokens = _exchange_code_for_tokens(settings.viessmann_client_id, redirect_uri, code, verifier)

    print("Suche nach deiner Vitocharge-Installation ...")
    installation_id, gateway_serial, device_id = _discover_device(tokens["access_token"])

    settings.viessmann_config_dir.mkdir(parents=True, exist_ok=True)
    auth_file = settings.viessmann_auth_file
    auth_file.write_text(
        json.dumps(
            {
                "client_id": settings.viessmann_client_id,
                "access_token": tokens["access_token"],
                "refresh_token": tokens["refresh_token"],
                "installation_id": installation_id,
                "gateway_serial": gateway_serial,
                "device_id": device_id,
            },
            indent=2,
        )
    )
    auth_file.chmod(0o600)

    print(f"\nGespeichert unter {auth_file}")
    print("Für den Pi: diese Datei per scp an den gleichen Pfad kopieren (oder")
    print("PILOADING_VIESSMANN_CONFIG_DIR entsprechend setzen). Sie enthält Zugangsdaten -")
    print("nicht committen (siehe .gitignore).")
