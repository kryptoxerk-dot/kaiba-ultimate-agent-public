"""Deployment boundaries for autonomous research; Codex AUTOLEARN ownership.

These controls catch accidental financial transports even when research fixtures pass.
The unit's network sandbox is the second boundary beyond the source import/call audit.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import pytest

from kaiba.learning import autolearn

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "systemd" / "kaiba-autolearn.service"
TIMER = ROOT / "deploy" / "systemd" / "kaiba-autolearn.timer"
RESEARCH_SOURCES = tuple(ROOT / "kaiba" / "learning" / (name + ".py")
                         for name in ("autolearn", "autolearn_io", "autolearn_discovery", "candidate_paper",
                                      "agent_review", "autolearn_review", "autolearn_wallets")) + (
                                          ROOT / "kaiba/intelligence/wallet_candidates.py",)


def _fields(text: str) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = sections.setdefault(line[1:-1], {})
        elif section is not None and "=" in line:
            key, value = line.split("=", 1)
            section.setdefault(key, []).append(value)
    return sections


def _seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|sec|min|m)?", value)
    assert match, "hard deadline must be a finite duration"
    return float(match[1]) * (60 if match[2] in ("m", "min") else 1)


def _deployment_contract(service: str, timer: str) -> None:
    fields = _fields(service)["Service"]
    assert fields.get("Type") == ["oneshot"], "research worker must finish a bounded single cycle"
    assert fields.get("RestrictAddressFamilies") == ["AF_UNIX"], "research unit must deny internet socket families"
    assert fields.get("NoNewPrivileges") == ["true"], "research process must not escape its sandbox"
    assert "EnvironmentFile" not in fields, "research must not inherit a credential environment file"
    environment = " ".join(fields.get("Environment", []))
    assert not re.search(r"(?i)(?:gmgn|hermes|token|secret|private|api_key|allow_automated)", environment), \
        "research must not receive financial transport credentials or automation consent"
    commands = fields.get("ExecStart", [])
    assert len(commands) == 1, "unit must run exactly one research command"
    args = shlex.split(commands[0])
    assert Path(args[0]).name == "timeout", "hard external timeout must contain the whole worker"
    assert args[1] in ("-k", "--kill-after"), "timeout must terminate a worker that ignores TERM"
    assert 0 < _seconds(args[2]) <= 10, "forced kill must follow the deadline promptly"
    assert 0 < _seconds(args[3]) + _seconds(args[2]) <= 1200, "whole source command must finish within 20 minutes"
    assert Path(args[4]).name == "nice" and args[5:7] == ["-n", "19"], "source work must use nice 19"
    assert Path(args[7]).name == "ionice" and args[8] == "-c3", "source I/O must run in idle class"
    assert Path(args[9]).name in ("python", "python3", "python3.12"), "worker must use the Python runtime"
    assert args[10:13] == ["-m", "kaiba.learning.autolearn", "--once"], "unit may run only the research CLI"
    assert args[13:] == ["--admission-clock"], "only the frozen research clock upgrade is allowed"
    assert fields.get("Nice") == ["19"] and fields.get("IOSchedulingClass") == ["idle"]
    assert len(fields.get("TimeoutStartSec", [])) == 1 and _seconds(fields["TimeoutStartSec"][0]) <= 1200
    memory = fields.get("MemoryMax", [""])[0]
    match = re.fullmatch(r"(\d+)(M|G)", memory)
    assert match and 0 < int(match[1]) * (1024 if match[2] == "G" else 1) <= 350, \
        "research must not exhaust service memory on the live host"
    assert all(not key.startswith(("ExecStartPre", "ExecStartPost", "ExecStop", "ExecReload")) for key in fields), \
        "research unit must not restart or mutate another live service"
    assert not re.search(r"(?i)\b(?:systemctl|restart|kaiba\.ops\.scheduler|hermes)\b", commands[0])
    schedule = _fields(timer)["Timer"]
    assert schedule.get("Unit") == ["kaiba-autolearn.service"]
    assert len(schedule.get("OnUnitInactiveSec", [])) == 1 and _seconds(schedule["OnUnitInactiveSec"][0]) == 15
    assert "OnUnitActiveSec" not in schedule, "timer must wait for completion before another research cycle"


def _transport_problems(source: str) -> list[str]:
    """A narrow additive worker may use its SQLite/broker APIs, never financial transports."""
    tree = ast.parse(source)
    problems = []
    network_modules = {"socket", "httpx", "requests", "aiohttp", "urllib", "websockets", "subprocess"}
    forbidden_calls = {"submit", "submit_gmgn", "swap", "withdraw", "transfer", "sign", "sign_transaction",
                       "sign_and_send_transaction", "_run_gmgn", "run_read", "get_json", "post_json",
                       "__import__", "eval", "exec", "open_connection", "open_unix_connection"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if (name.name.split(".")[0] in network_modules
                        or name.name.startswith(("kaiba.execution.executor", "kaiba.execution.signer", "kaiba.providers"))):
                    problems.append("transport import: " + name.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "kaiba.providers.native_price":
                if any(name.name != "price_source_chain" for name in node.names):
                    problems.append("native price may be used only for the pure chain alias")
            elif (module.split(".")[0] in network_modules
                  or module.startswith(("kaiba.execution.executor", "kaiba.execution.signer", "kaiba.providers"))):
                problems.append("transport import: " + module)
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name in forbidden_calls:
                problems.append("transport call: " + name)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os":
                if func.attr in ("system", "popen") or func.attr.startswith(("spawn", "exec")):
                    problems.append("process transport: os." + func.attr)
                if func.attr == "kill" and (len(node.args) != 2 or not isinstance(node.args[1], ast.Constant)
                                             or node.args[1].value != 0):
                    problems.append("research may inspect a lock PID, never signal a live process")
            if name in ("buy", "sell") and (not isinstance(func, ast.Attribute)
                                            or not isinstance(func.value, ast.Name) or func.value.id != "broker"):
                problems.append("financial calls must be through the existing paper broker")
    return problems


def test_real_deployment_has_two_financial_transport_boundaries_and_host_resource_bounds():
    _deployment_contract(UNIT.read_text(encoding="utf-8"), TIMER.read_text(encoding="utf-8"))
    for path in RESEARCH_SOURCES:
        assert _transport_problems(path.read_text(encoding="utf-8")) == [], path.name


def test_network_sandbox_removed_mutation_is_caught_independently_of_research_fixtures():
    service, timer = UNIT.read_text(encoding="utf-8"), TIMER.read_text(encoding="utf-8")
    _deployment_contract(service, timer)
    mutant = service.replace("RestrictAddressFamilies=AF_UNIX", "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX")
    assert mutant != service
    with pytest.raises(AssertionError, match="deny internet socket families"):
        _deployment_contract(mutant, timer)


@pytest.mark.parametrize("unsafe", [
    "from kaiba.execution.executor import submit\nsubmit(None)",
    "from kaiba.execution.signer import sign_transaction\nsign_transaction(None)",
    "from kaiba.providers.native_price import sample\nsample(None)",
    "import socket\nsocket.socket()",
    "import subprocess\nsubprocess.run(['systemctl','restart','kaiba-ops'])",
    "import os\nos.system('curl https://example.invalid')",
    "__import__('kaiba.execution.executor').submit(None)",
])
def test_transport_added_mutations_are_caught_without_executing_them(unsafe):
    assert _transport_problems("from kaiba.providers.native_price import price_source_chain\nprice_source_chain(None)") == []
    assert _transport_problems(unsafe), unsafe


@pytest.mark.parametrize("kind", ["relative", "absolute", "nested"])
def test_manifest_path_escape_is_refused_before_reading_an_external_file(tmp_path, monkeypatch, kind):
    research = tmp_path / "learning"
    learner = autolearn.AutoLearner(tmp_path / "source.db", research / "worker", learning_root=research)
    outside = research / "outside.json"
    outside.write_text('{"never_read_external":"sentinel"}', encoding="utf-8")
    name = "../outside.json" if kind == "relative" else str(outside) if kind == "absolute" else "paper/../../outside.json"
    loaded = []

    def observe(path, default):
        loaded.append(Path(path).resolve())
        raise AssertionError("external manifest was read before path admission")

    monkeypatch.setattr(autolearn, "_load", observe)
    with pytest.raises(ValueError, match="campaign"):
        learner._bootstrap({"campaign_files": [name]}, 1_800_000_000_000)
    assert loaded == []


def test_valid_owned_campaign_name_has_a_positive_admission_control(tmp_path, monkeypatch):
    research = tmp_path / "learning"
    learner = autolearn.AutoLearner(tmp_path / "source.db", research / "worker", learning_root=research)
    owned = learner.state_dir / "owned-campaign.json"
    from kaiba.learning.strategy_campaign import source_versions
    packet = {"campaign_id": "owned", "hypotheses": [], "source_versions": source_versions()}
    observed = []

    def observe(path, default):
        observed.append(Path(path))
        return packet

    monkeypatch.setattr(autolearn, "_load", observe)
    assert learner._bootstrap({"campaign_files": [owned.name]}, 1_800_000_000_000) == [packet]
    assert observed == [owned]


def test_escape_guard_removed_mutation_trips_the_independent_external_read_witness(tmp_path, monkeypatch):
    monkeypatch.setattr(autolearn.AutoLearner, "_campaign_path", lambda self, name: self.state_dir / name)
    with pytest.raises(AssertionError, match="external manifest was read before path admission"):
        test_manifest_path_escape_is_refused_before_reading_an_external_file(tmp_path, monkeypatch, "relative")
