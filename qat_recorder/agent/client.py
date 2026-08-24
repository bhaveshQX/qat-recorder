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
from urllib.parse import quote

from qat_recorder.agent.protocol import (
    AgentError, Busy, MAX_POLL_SECONDS, check_protocol, route,
)
from qat_recorder.agent.security import client_context, peer_fingerprint
from qat_recorder.replay import DEFAULT_TIMEOUT


class AgentClient:
    def __init__(self, host: str, port: int, token: str,
                 fingerprint: Optional[str] = None,
                 ca_file: Optional[str] = None,
                 owner: str = "",
                 timeout: float = 40.0,
                 insecure_plaintext: bool = False,
                 ngrok_url: str = ""):
        self.host = host
        self.port = port
        self.token = token
        self.fingerprint = (fingerprint or "").replace(":", "").lower() or None
        self.ca_file = ca_file
        self.owner = owner or "unknown"
        self.timeout = timeout
        self.insecure_plaintext = insecure_plaintext
        self.ngrok_url = ngrok_url

        if ngrok_url:
            from urllib.parse import urlparse
            parsed = urlparse(ngrok_url)
            self.host = parsed.hostname
            self.port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            self.insecure_plaintext = (parsed.scheme == 'http')
            import ssl
            self._context = None if self.insecure_plaintext else ssl.create_default_context()
        elif insecure_plaintext:
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
        if self.fingerprint and not self.ngrok_url:
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

    def keep(self, session_id: str, name: str, verify: bool = True) -> dict:
        """Keep the session as a named test on the host, which runs it once.

        The run happens on the host, so this call holds for as long as the test
        takes -- which is the point: the answer comes back with the name.
        """
        previous = self.timeout
        self.timeout = max(self.timeout, DEFAULT_TIMEOUT + 30.0)
        try:
            return self._request("POST", route("keep", session_id=session_id),
                                 {"name": name, "verify": verify})["test"]
        finally:
            self.timeout = previous

    def tests(self, app: str = "") -> dict:
        path = route("tests") + (f"?app={quote(app)}" if app else "")
        return self._request("GET", path)

    def run_test(self, test_id: str, timeout: float = 0.0) -> dict:
        previous = self.timeout
        self.timeout = max(self.timeout, (timeout or DEFAULT_TIMEOUT) + 30.0)
        try:
            return self._request("POST", route("run"),
                                 {"test": test_id, "timeout": timeout})
        finally:
            self.timeout = previous

    def replay(self, session_id: str, timeout: float = 0.0) -> dict:
        """Run the generated test on the host, and wait for the verdict.

        A replay takes as long as the application takes to start and be driven,
        which is longer than any other call here, so the socket timeout is
        raised for this one request rather than for every request.
        """
        previous = self.timeout
        self.timeout = max(self.timeout, (timeout or DEFAULT_TIMEOUT) + 30.0)
        try:
            return self._request("POST", route("replay", session_id=session_id),
                                 {"timeout": timeout})
        except AgentError as error:
            if error.status == 404:
                raise AgentError(
                    "this agent is too old to replay; update qat_recorder on "
                    "the host and restart the agent") from error
            raise
        finally:
            self.timeout = previous

    def release(self, session_id: str) -> dict:
        return self._request("DELETE", f"/v1/sessions/{session_id}")
