# -*- coding: utf-8 -*-
"""
The list of VMs a tester can record on.

Small on purpose: a JSON file of named hosts, each with the address, the pinned
certificate fingerprint, and where to find the token. Enough for a lab of
machines shared by a team; not a service-discovery system.

Tokens are referenced, not stored, by default. A token pasted into a config file
outnumbers every other way credentials leak, so `token_file` and `token_env` come
first and an inline token is written 0600 and flagged.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional

REGISTRY_VERSION = 1
DEFAULT_PORT = 8765


def config_dir() -> Path:
    """Where the host list lives, per platform convention."""
    override = os.environ.get("QATREC_CONFIG_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "qat-recorder"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "qat-recorder"


def registry_path() -> Path:
    return config_dir() / "hosts.json"


@dataclass
class Host:
    name: str
    host: str
    port: int = DEFAULT_PORT
    fingerprint: str = ""
    ca_file: str = ""
    token_file: str = ""
    token_env: str = ""
    token: str = ""            # discouraged; see module docstring
    ngrok_url: str = ""
    insecure_plaintext: bool = False
    note: str = ""

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def resolve_token(self) -> str:
        """Find the token, preferring the sources that do not sit in a file
        alongside the address they unlock."""
        if self.token_file:
            path = Path(self.token_file).expanduser()
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError(f"token file {path} is empty")
            return text
        if self.token_env:
            value = os.environ.get(self.token_env, "").strip()
            if not value:
                raise ValueError(
                    f"{self.token_env} is not set (host {self.name!r} expects "
                    "its token there)")
            return value
        if self.token:
            return self.token
        from qat_recorder.agent.security import TOKEN_ENV
        value = os.environ.get(TOKEN_ENV, "").strip()
        if value:
            return value
        raise ValueError(
            f"no token for host {self.name!r}: set token_file, token_env, or "
            f"the {TOKEN_ENV} environment variable")

    def validate(self) -> list:
        """Problems worth telling the operator about before they connect."""
        problems = []
        if not self.host:
            problems.append("no address")
        if not (1 <= int(self.port) <= 65535):
            problems.append(f"port {self.port} is out of range")
        if not self.insecure_plaintext and not self.fingerprint and \
                not self.ca_file:
            problems.append(
                "no fingerprint and no CA: the agent's identity cannot be "
                "checked")
        if self.token:
            problems.append(
                "token is stored inline; prefer token_file or token_env")
        return problems


@dataclass
class Registry:
    hosts: Dict[str, Host] = field(default_factory=dict)
    path: Optional[Path] = None

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Registry":
        path = Path(path) if path else registry_path()
        if not path.exists():
            return cls(hosts={}, path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"{path} is not valid JSON: {error}")

        version = int(data.get("version", 0))
        if version != REGISTRY_VERSION:
            raise ValueError(
                f"{path} uses registry version {version}; this build reads "
                f"{REGISTRY_VERSION}")

        hosts = {}
        for name, entry in (data.get("hosts") or {}).items():
            fields = {key: entry.get(key) for key in Host.__annotations__
                      if key in entry}
            fields["name"] = name
            hosts[name] = Host(**fields)
        return cls(hosts=hosts, path=path)

    def save(self) -> Path:
        path = Path(self.path) if self.path else registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": REGISTRY_VERSION,
            "hosts": {
                name: {key: value for key, value in asdict(host).items()
                       if key != "name" and value not in ("", 0, False)}
                for name, host in sorted(self.hosts.items())
            },
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        # Any inline token makes this file a credential.
        if any(host.token for host in self.hosts.values()):
            try:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        self.path = path
        return path

    # -- access ------------------------------------------------------------

    def add(self, host: Host) -> Host:
        self.hosts[host.name] = host
        return host

    def remove(self, name: str) -> bool:
        return self.hosts.pop(name, None) is not None

    def names(self) -> list:
        return sorted(self.hosts)

    def get(self, name: str) -> Host:
        try:
            return self.hosts[name]
        except KeyError:
            known = ", ".join(self.names()) or "none registered"
            raise KeyError(f"no host named {name!r} (known: {known})")

    def resolve(self, reference: str) -> Host:
        """Accept a registered name, or a bare `host` / `host:port`.

        Typing an address directly stays possible so a one-off machine does not
        have to be registered first.
        """
        if reference in self.hosts:
            return self.hosts[reference]
        if ":" in reference:
            host, _, port = reference.rpartition(":")
            if port.isdigit():
                return Host(name=reference, host=host, port=int(port))
        if "." in reference or reference in ("localhost", "127.0.0.1"):
            return Host(name=reference, host=reference)
        return self.get(reference)


def client_for(host: Host, owner: str = "", timeout: float = 40.0):
    """Build a connected client for a registry entry."""
    from qat_recorder.agent.client import AgentClient

    return AgentClient(
        host.host, int(host.port), host.resolve_token(),
        fingerprint=host.fingerprint or None,
        ca_file=host.ca_file or None,
        owner=owner or _current_user(),
        timeout=timeout,
        insecure_plaintext=bool(host.insecure_plaintext),
        ngrok_url=host.ngrok_url)


def _current_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:                                        # noqa: BLE001
        return "unknown"
