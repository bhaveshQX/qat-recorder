# -*- coding: utf-8 -*-
"""Tests for the host registry and the hosts CLI."""

import json
import os

import pytest

from qat_recorder.agent.registry import (
    DEFAULT_PORT, Host, Registry, client_for, config_dir, registry_path,
)


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    return Registry.load(tmp_path / "hosts.json")


# --- storage ---------------------------------------------------------------

def test_missing_registry_is_empty_not_an_error(tmp_path):
    assert Registry.load(tmp_path / "nothing.json").hosts == {}


def test_round_trip(registry):
    registry.add(Host(name="vm-01", host="10.0.0.5", port=9000,
                      fingerprint="ABCD", token_file="/etc/token",
                      note="HMI rig"))
    path = registry.save()

    reloaded = Registry.load(path)
    host = reloaded.get("vm-01")
    assert host.host == "10.0.0.5"
    assert host.port == 9000
    assert host.fingerprint == "ABCD"
    assert host.token_file == "/etc/token"
    assert host.note == "HMI rig"


def test_unknown_registry_version_is_refused(tmp_path):
    path = tmp_path / "hosts.json"
    path.write_text(json.dumps({"version": 99, "hosts": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="registry version 99"):
        Registry.load(path)


def test_corrupt_registry_says_so(tmp_path):
    path = tmp_path / "hosts.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        Registry.load(path)


def test_config_dir_honours_the_override(tmp_path, monkeypatch):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    assert config_dir() == tmp_path
    assert registry_path() == tmp_path / "hosts.json"


def test_unknown_host_lists_what_is_known(registry):
    registry.add(Host(name="vm-01", host="a"))
    registry.add(Host(name="vm-02", host="b"))
    with pytest.raises(KeyError, match="vm-01, vm-02"):
        registry.get("vm-99")


# --- resolution ------------------------------------------------------------

def test_a_registered_name_resolves(registry):
    registry.add(Host(name="vm-01", host="10.0.0.5", port=9000))
    assert registry.resolve("vm-01").port == 9000


def test_a_bare_address_resolves_without_registration(registry):
    """A one-off machine should not have to be registered first."""
    host = registry.resolve("10.0.0.9:8123")
    assert host.host == "10.0.0.9"
    assert host.port == 8123


def test_a_bare_hostname_uses_the_default_port(registry):
    assert registry.resolve("build.example.com").port == DEFAULT_PORT


def test_a_registered_name_wins_over_address_parsing(registry):
    registry.add(Host(name="localhost", host="10.0.0.5", port=1234))
    assert registry.resolve("localhost").port == 1234


# --- tokens ----------------------------------------------------------------

def test_token_file_is_preferred(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("  from-file  \n", encoding="utf-8")
    host = Host(name="v", host="h", token_file=str(token_file),
                token="inline", token_env="NOPE")
    assert host.resolve_token() == "from-file"


def test_token_env_is_used_when_named(monkeypatch):
    monkeypatch.setenv("MY_AGENT_TOKEN", "from-env")
    host = Host(name="v", host="h", token_env="MY_AGENT_TOKEN", token="inline")
    assert host.resolve_token() == "from-env"


def test_missing_token_env_names_the_variable(monkeypatch):
    monkeypatch.delenv("MY_AGENT_TOKEN", raising=False)
    host = Host(name="v", host="h", token_env="MY_AGENT_TOKEN")
    with pytest.raises(ValueError, match="MY_AGENT_TOKEN"):
        host.resolve_token()


def test_empty_token_file_is_an_error(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError, match="is empty"):
        Host(name="v", host="h", token_file=str(token_file)).resolve_token()


def test_no_token_anywhere_explains_the_options(monkeypatch):
    monkeypatch.delenv("QATREC_AGENT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token_file"):
        Host(name="v", host="h").resolve_token()


# --- validation ------------------------------------------------------------

def test_a_host_without_a_pin_is_flagged():
    problems = Host(name="v", host="10.0.0.5").validate()
    assert any("identity cannot be checked" in p for p in problems)


def test_an_inline_token_is_flagged():
    problems = Host(name="v", host="h", fingerprint="ab",
                    token="secret").validate()
    assert any("stored inline" in p for p in problems)


def test_plaintext_host_needs_no_pin():
    problems = Host(name="v", host="127.0.0.1", insecure_plaintext=True,
                    token_file="/x").validate()
    assert not any("identity" in p for p in problems)


def test_a_bad_port_is_flagged():
    assert any("out of range" in p
               for p in Host(name="v", host="h", port=70000,
                             fingerprint="ab").validate())


def test_inline_token_file_is_written_private(registry):
    registry.add(Host(name="v", host="h", token="secret"))
    path = registry.save()
    if os.name != "nt":
        assert (path.stat().st_mode & 0o077) == 0


# --- client construction ---------------------------------------------------

def test_client_carries_the_pin_and_owner(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("tok", encoding="utf-8")
    host = Host(name="vm", host="10.0.0.5", port=9001,
                fingerprint="AB:CD", token_file=str(token_file))

    client = client_for(host, owner="alice")
    assert client.host == "10.0.0.5"
    assert client.port == 9001
    assert client.token == "tok"
    assert client.fingerprint == "abcd"      # normalised
    assert client.owner == "alice"


def test_client_for_a_plaintext_host_skips_tls(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("tok", encoding="utf-8")
    host = Host(name="vm", host="127.0.0.1", token_file=str(token_file),
                insecure_plaintext=True)
    assert client_for(host)._context is None


# --- the hosts CLI ---------------------------------------------------------

def test_hosts_cli_add_list_remove(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    from qat_recorder.cli import main

    assert main(["hosts", "add", "vm-01", "10.0.0.5:8765",
                 "--fingerprint", "AB:CD", "--token-file", "/etc/token"]) == 0
    assert main(["hosts", "list"]) == 0
    assert "vm-01" in capsys.readouterr().out

    assert main(["hosts", "remove", "vm-01"]) == 0
    assert main(["hosts", "list"]) == 0
    assert "no hosts registered" in capsys.readouterr().out


def test_hosts_cli_warns_about_an_unverified_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    from qat_recorder.cli import main

    assert main(["hosts", "add", "vm-02", "10.0.0.6"]) == 1
    assert "identity cannot be checked" in capsys.readouterr().err


def test_hosts_cli_removing_an_unknown_host_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("QATREC_CONFIG_DIR", str(tmp_path))
    from qat_recorder.cli import main
    assert main(["hosts", "remove", "ghost"]) == 1
