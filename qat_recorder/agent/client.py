# -*- coding: utf-8 -*-
"""
Client side of the agent protocol.

Small and synchronous on purpose: it is driven either by a controller adapter or
by the CLI, and the event stream is long-polled rather than pushed, so there is
nothing to gain from async here.
"""

from __future__ import annotations

import hmac
import http.client
import json
from typing import Optional

from qat_recorder.agent.protocol import (
    AgentError, Busy, MAX_POLL_SECONDS, check_protocol, route,
)
from qat_recorder.agent.security import client_context, peer_fingerprint


class AgentClient:
    def __init__(self, host: str, port: int, token: str,
                 fingerprint: Optional[str] = None,
                 ca_file: Optional[str] = None,
                 owner: str = "",
                 timeout: float = 40.0,
                 insecure_plaintext: bool = False):
        self.host = host
        self.port = port
        self.token = token
        self.fingerprint = (fingerprint or "").replace(":", "").lower() or None
        self.ca_file = ca_file
        self.owner = owner or "unknown"
        self.timeout = timeout
        self.insecure_plaintext = insecure_plaintext

        if insecure_plaintext:
            self._context = None
        else:
            self._context = client_context(self.fingerprint, ca_file)

    # -- transport ---------------------------------------------------------

    def _connect(self):
        if self._context is None:
            return http.client.HTTPConnection(
                self.host, self.port, timeout=self.timeout)
        connection = http.client.HTTPSConnection(
            self.host, self.port, context=self._context, timeout=self.timeout)
        connection.connect()
        if self.fingerprint:
            # Python cannot express pinning inside an SSLContext, so it is
            # checked here, after the handshake and before anything is sent.
            actual = peer_fingerprint(connection.sock)
            if not hmac.compare_digest(actual, self.fingerprint):
                connection.close()
                raise AgentError(
                    "certificate fingerprint does not match the pin; expected "
                    f"{self.fingerprint}, got {actual}")
        return connection

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"

        connection = self._connect()
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read().decode("utf-8")
            status = response.status
        finally:
            connection.close()

        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            raise AgentError(f"agent returned non-JSON ({status}): {raw[:200]}",
                             status)

        if status == 401:
            raise AgentError("agent rejected the token", 401)
        if status == 409 and "owner" in (parsed.get("detail") or {}):
            detail = parsed["detail"]
            raise Busy(detail.get("owner", "someone"), detail.get("since", "?"),
                       detail.get("session_id", ""))
        if status >= 400:
            raise AgentError(parsed.get("error", f"agent error {status}"),
                             status, parsed.get("detail"))

        check_protocol(parsed)
        return parsed

    # -- calls -------------------------------------------------------------

    def health(self) -> dict:
        return self._request("GET", route("health"))

    def applications(self) -> list:
        return self._request("GET", route("applications"))["applications"]

    def current(self) -> Optional[dict]:
        return self._request("GET", route("sessions")).get("session")

    def start(self, app: str, lib: str, name: str = "") -> dict:
        return self._request("POST", route("sessions"), {
            "app": app, "lib": lib, "name": name, "owner": self.owner})

    def events(self, session_id: str, since: int = 0,
               wait: float = 5.0) -> dict:
        wait = max(0.0, min(wait, MAX_POLL_SECONDS))
        path = f"{route('events', session_id=session_id)}?since={since}&wait={wait}"
        return self._request("GET", path)

    def command(self, session_id: str, command: str,
                args: Optional[dict] = None) -> dict:
        return self._request("POST", route("command", session_id=session_id),
                             {"command": command, "args": args or {}})

    def artifacts(self, session_id: str) -> dict:
        return self._request("GET", route("artifacts", session_id=session_id))

    def release(self, session_id: str) -> dict:
        return self._request("DELETE", f"/v1/sessions/{session_id}")
