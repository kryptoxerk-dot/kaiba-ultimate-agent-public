"""Exercise exact installer program on isolated files; no SSH/systemd/HTTP."""
import base64
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def module():
    spec = importlib.util.spec_from_file_location("grok_install", Path(__file__).parents[1]/"deploy/grok-alpha-install.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def fixture(tmp_path, monkeypatch, *, apply=False, invalid=False):
    installer = module()
    root, home, etc = tmp_path/"kaiba", tmp_path/"home", tmp_path/"etc"
    root.mkdir()
    home.mkdir()
    etc.mkdir()
    caddy = etc/"Caddyfile"
    original = b"agent.example.com {\n    encode gzip\n    reverse_proxy 127.0.0.1:8794\n}\n"
    caddy.write_bytes(original)
    for name in ("risk.yaml", "signer-policy.yaml", "schedule.yaml"):
        (root/"config").mkdir(exist_ok=True)
        (root/"config"/name).write_text("unchanged")
    files = {}
    for name in installer.FILES:
        raw = ("committed " + name).encode()
        files[name] = {"base64": base64.b64encode(raw).decode(), "sha256": hashlib.sha256(raw).hexdigest()}
    data = {"expected_caddy": hashlib.sha256(original).hexdigest(), "public_key": "a"*64,
            "files": files, "commit": "test", "apply": apply}
    code = installer.REMOTE.replace("Path('/home/ubuntu/kaiba')", f"Path({str(root)!r})")
    code = code.replace("Path('/etc/caddy/Caddyfile')", f"Path({str(caddy)!r})")
    code = code.replace("Path('/etc/caddy/grok-alpha.caddy')", f"Path({str(etc/'grok-alpha.caddy')!r})")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if "install" in args:
            shutil.copyfile(args[-2], args[-1])
        if "mv" in args:
            Path(args[-2]).replace(args[-1])
        failed = invalid and "validate" in args
        return subprocess.CompletedProcess(args, 1 if failed else 0,
                    stdout="123\n" if "--value" in args else "MainPID=123\nActiveState=active\nSubState=running\n" if "show" in args else "", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    return code, data, root, caddy, original, commands


def test_check_mode_does_not_install_sources_or_change_config(tmp_path, monkeypatch, capsys):
    code, data, root, caddy, original, commands = fixture(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as result:
        exec(compile(code, "installer", "exec"), {})
    assert result.value.code == 0
    assert caddy.read_bytes() == original
    assert not (root/"kaiba").exists()
    assert not any("enable" in c for c in commands)
    assert json.loads(capsys.readouterr().out)["check_passed"] is True


def test_changed_caddy_or_existing_source_aborts_before_mutation(tmp_path, monkeypatch):
    code, data, root, caddy, original, commands = fixture(tmp_path, monkeypatch, apply=True)
    caddy.write_bytes(original+b"# other's edit\n")
    with pytest.raises(RuntimeError, match="caddy_source_changed"):
        exec(compile(code, "installer", "exec"), {})
    assert not commands
    caddy.write_bytes(original)
    owned = root/"kaiba/hunters/grok_alpha_bridge.py"
    owned.parent.mkdir(parents=True)
    owned.write_text("other collaborator's work")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    with pytest.raises(RuntimeError, match="existing_owned_source_differs"):
        exec(compile(code, "installer", "exec"), {})
    assert caddy.read_bytes() == original


def test_invalid_proxy_config_never_replaces_live_config_or_starts_service(tmp_path, monkeypatch):
    code, data, root, caddy, original, commands = fixture(tmp_path, monkeypatch, apply=True, invalid=True)
    with pytest.raises(RuntimeError, match="command_failed"):
        exec(compile(code, "installer", "exec"), {})
    assert caddy.read_bytes() == original
    assert not any("enable" in command for command in commands)


def test_apply_preserves_original_proxy_and_other_services(tmp_path, monkeypatch, capsys):
    code, data, root, caddy, original, commands = fixture(tmp_path, monkeypatch, apply=True)
    exec(compile(code, "installer", "exec"), {})
    out = json.loads(capsys.readouterr().out)
    assert out["financial_config_before"] == out["financial_config_after"]
    assert out["gateway_and_protection_pids_before"] == out["gateway_and_protection_pids_after"]
    assert b"reverse_proxy 127.0.0.1:8794" in caddy.read_bytes()
    assert b"import /etc/caddy/grok-alpha.caddy" in caddy.read_bytes()
    assert [c for c in commands if "enable" in c] == [["systemctl", "--user", "enable", "--now", "kaiba-grok-alpha.service"]]
    assert not any("restart" in c for c in commands)


def test_repair_requires_exact_prior_unit_and_restarts_only_new_service(tmp_path, monkeypatch, capsys):
    code, data, root, caddy, original, commands = fixture(tmp_path, monkeypatch, apply=True)
    source = root/"deploy/systemd/kaiba-grok-alpha.service"
    source.parent.mkdir(parents=True)
    old = b"previous committed unit"
    source.write_bytes(old)
    data["prior_unit_sha"] = "0"*64
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    with pytest.raises(RuntimeError, match="existing_owned_source_differs"):
        exec(compile(code, "installer", "exec"), {})
    assert source.read_bytes() == old
    data["prior_unit_sha"] = hashlib.sha256(old).hexdigest()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    exec(compile(code, "installer", "exec"), {})
    assert [c for c in commands if "restart" in c] == [["systemctl", "--user", "restart", "kaiba-grok-alpha.service"]]
    out = json.loads(capsys.readouterr().out)
    assert out["financial_config_before"] == out["financial_config_after"]
