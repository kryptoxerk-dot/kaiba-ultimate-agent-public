"""`kaiba signer keygen` / `kaiba signer import` (design review 2026-10-02, defect 3).

install.sh and the keys runbook documented `signer keygen`; the CLI had only `signer serve`.
These pin the contract: the key is created inside the keystore with 0600 and O_EXCL, an
existing key is never overwritten, the ONLY output is the public address, and no form of
the key material reaches stdout, stderr or the log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

import base58
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from kaiba.cli.main import _read_secret_hidden, app
from kaiba.core.schemas import Chain
from kaiba.execution import signer
from tests.signer_fixtures import KNOWN_EVM_ADDRESS, KNOWN_EVM_KEY

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()

# RFC 8032 section 7.1, TEST 1.
RFC_SEED = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC_PUB = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC_SIG_EMPTY = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)


def cli(*args: str, input: str | None = None):
    return runner.invoke(app, ["signer", *args], input=input)


def sol_leaks(keypair: bytes) -> list[str]:
    return [
        base58.b58encode(keypair).decode(),
        base58.b58encode(keypair[:32]).decode(),
        keypair[:32].hex(),
        keypair.hex(),
        json.dumps(list(keypair)),
    ]


def evm_leaks(k: int) -> list[str]:
    h = k.to_bytes(32, "big").hex()
    return [h, h.upper(), str(k)]


def assert_no_leak(leaks: list[str], *texts: str) -> None:
    for text in texts:
        for leak in leaks:
            assert leak not in text


@pytest.fixture
def keystore(tmp_path):
    return tmp_path / "keys"


# ---------------------------------------------------------------- known answers


def test_evm_address_derivation_matches_the_published_vector():
    assert signer._evm_address(KNOWN_EVM_KEY) == KNOWN_EVM_ADDRESS


def test_sol_key_matches_rfc8032():
    assert signer._sol_address(RFC_SEED + RFC_PUB) == base58.b58encode(RFC_PUB).decode()
    assert signer._SolKey(RFC_SEED + RFC_PUB).sign(b"") == RFC_SIG_EMPTY
    with pytest.raises(signer.KeyFormatError, match="does not match"):
        signer._sol_address(RFC_SEED + bytes(32))


# ---------------------------------------------------------------- keygen


def test_sol_keygen_prints_only_the_address(keystore, caplog):
    caplog.set_level(logging.DEBUG)
    r = cli("keygen", "--chain", "sol", "--keystore", str(keystore))
    assert r.exit_code == 0, r.output
    address = r.stdout.strip()
    assert r.stdout == address + "\n" and r.stderr == ""
    assert [p.name for p in keystore.iterdir()] == [f"sol-{address}.key"]
    keypair = base58.b58decode((keystore / f"sol-{address}.key").read_text().strip())
    assert base58.b58encode(keypair[32:]).decode() == address
    assert_no_leak(sol_leaks(keypair), r.stdout, r.stderr, caplog.text)
    key = signer.Keystore(keystore).load(Chain.SOL, address)
    assert key.verify(key.sign(b"m"), b"m")


def test_evm_keygen_prints_only_the_checksummed_address(keystore, caplog):
    caplog.set_level(logging.DEBUG)
    r = cli("keygen", "--chain", "evm", "--keystore", str(keystore))
    assert r.exit_code == 0, r.output
    address = r.stdout.strip()
    assert re.fullmatch(r"0x[0-9a-fA-F]{40}", address) and r.stderr == ""
    k = int((keystore / f"evm-{address.lower()}.key").read_text().strip(), 16)
    assert signer._evm_address(k) == address
    assert_no_leak(evm_leaks(k), r.stdout, r.stderr, caplog.text)


def test_the_key_file_is_created_exclusively_with_mode_0600(keystore, monkeypatch):
    calls: list[tuple[str, int, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        calls.append((str(path), flags, mode))
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy)
    signer.keygen("sol", keystore)
    key_opens = [c for c in calls if c[0].endswith(".key")]
    assert len(key_opens) == 1
    _path, flags, mode = key_opens[0]
    assert mode == 0o600
    assert flags & os.O_CREAT and flags & os.O_EXCL


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_key_file_and_directory_permissions_on_posix(keystore):
    old = os.umask(0o022)  # a permissive umask must not widen the key file
    try:
        address = signer.keygen("sol", keystore)
    finally:
        os.umask(old)
    assert (keystore / f"sol-{address}.key").stat().st_mode & 0o777 == 0o600
    assert keystore.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_group_readable_keystore_is_refused(keystore):
    keystore.mkdir(mode=0o755)
    keystore.chmod(0o755)
    with pytest.raises(signer.SignerRefused, match="group/world accessible"):
        signer.keygen("sol", keystore)


def test_keygen_refuses_to_overwrite(keystore, monkeypatch):
    fixed = signer._new_sol_keypair()
    monkeypatch.setattr(signer, "_new_sol_keypair", lambda: fixed)
    first = cli("keygen", "--chain", "sol", "--keystore", str(keystore))
    assert first.exit_code == 0
    path = keystore / f"sol-{first.stdout.strip()}.key"
    before = path.read_bytes()
    second = cli("keygen", "--chain", "sol", "--keystore", str(keystore))
    assert second.exit_code == 1
    assert "refusing to overwrite" in second.output
    assert path.read_bytes() == before
    assert_no_leak(sol_leaks(fixed), second.output)


def test_keygen_without_a_keystore_fails_rather_than_guessing(monkeypatch):
    monkeypatch.delenv("KAIBA_KEYSTORE_DIR", raising=False)
    r = cli("keygen", "--chain", "sol")
    assert r.exit_code == 1 and "no keystore directory" in r.output


def test_keygen_reads_the_keystore_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIBA_KEYSTORE_DIR", str(tmp_path / "from-env"))
    r = cli("keygen", "--chain", "sol")
    assert r.exit_code == 0
    assert (tmp_path / "from-env" / f"sol-{r.stdout.strip()}.key").exists()


def test_an_unknown_key_family_is_refused(keystore):
    r = cli("keygen", "--chain", "btc", "--keystore", str(keystore))
    assert r.exit_code == 1 and "unknown key family" in r.output
    assert not keystore.exists()


# ---------------------------------------------------------------- import


def test_import_sol_from_stdin_prints_only_the_address(keystore, caplog):
    caplog.set_level(logging.DEBUG)
    keypair = RFC_SEED + RFC_PUB
    r = cli("import", "--chain", "sol", "--keystore", str(keystore),
            input=base58.b58encode(keypair).decode() + "\n")
    assert r.exit_code == 0, r.output
    address = base58.b58encode(RFC_PUB).decode()
    assert r.stdout == address + "\n"
    assert_no_leak(sol_leaks(keypair), r.stdout, r.stderr, caplog.text)
    assert signer.Keystore(keystore).load(Chain.SOL, address).sign(b"") == RFC_SIG_EMPTY


def test_import_accepts_the_solana_cli_json_array(keystore):
    keypair = RFC_SEED + RFC_PUB
    r = cli("import", "--chain", "sol", "--keystore", str(keystore), input=json.dumps(list(keypair)) + "\n")
    assert r.exit_code == 0 and r.stdout.strip() == base58.b58encode(RFC_PUB).decode()


@pytest.mark.parametrize("form", ["0x{h}", "{h}", "0X{H}"])
def test_import_evm_hex(keystore, caplog, form):
    caplog.set_level(logging.DEBUG)
    h = KNOWN_EVM_KEY.to_bytes(32, "big").hex()
    r = cli("import", "--chain", "evm", "--keystore", str(keystore), input=form.format(h=h, H=h.upper()) + "\n")
    assert r.exit_code == 0, r.output
    assert r.stdout == KNOWN_EVM_ADDRESS + "\n"
    assert (keystore / f"evm-{KNOWN_EVM_ADDRESS.lower()}.key").exists()
    assert_no_leak(evm_leaks(KNOWN_EVM_KEY), r.stdout, r.stderr, caplog.text)


def test_import_refuses_to_overwrite(keystore):
    secret = base58.b58encode(RFC_SEED + RFC_PUB).decode() + "\n"
    assert cli("import", "--chain", "sol", "--keystore", str(keystore), input=secret).exit_code == 0
    path = keystore / f"sol-{base58.b58encode(RFC_PUB).decode()}.key"
    before = path.read_bytes()
    again = cli("import", "--chain", "sol", "--keystore", str(keystore), input=secret)
    assert again.exit_code == 1 and "refusing to overwrite" in again.output
    assert path.read_bytes() == before
    assert_no_leak(sol_leaks(RFC_SEED + RFC_PUB), again.output)


@pytest.mark.parametrize(
    ("chain", "secret"),
    [
        ("sol", "notakeySECRETMATERIAL0lI"),  # base58-invalid characters
        ("sol", base58.b58encode(RFC_SEED + bytes(32)).decode()),  # halves do not match
        ("sol", json.dumps(list(RFC_SEED))),  # 32 numbers, not 64
        ("evm", "ab" * 31 + "SECRET"),
        ("evm", "00" * 32),  # zero is not a key
        ("evm", "ff" * 32),  # above the group order
    ],
)
def test_a_malformed_key_is_refused_without_echoing_it(keystore, caplog, chain, secret):
    caplog.set_level(logging.DEBUG)
    r = cli("import", "--chain", chain, "--keystore", str(keystore), input=secret + "\n")
    assert r.exit_code == 1 and "not a valid" in r.output
    for fragment in (secret, secret[:12], secret[-12:]):
        assert fragment not in r.output and fragment not in caplog.text
    assert not keystore.exists() or not any(keystore.iterdir())


def test_import_has_no_way_to_take_the_key_from_argv(keystore):
    command = get_command(app).commands["signer"].commands["import"]  # type: ignore[attr-defined]
    assert {p.name for p in command.params} == {"chain", "keystore"}
    r = cli("import", "--chain", "sol", "--keystore", str(keystore), base58.b58encode(RFC_SEED + RFC_PUB).decode())
    assert r.exit_code != 0
    assert not keystore.exists()


def test_a_terminal_gets_a_hidden_prompt_not_a_visible_read(monkeypatch):
    prompts: list[str] = []

    def fake_getpass(prompt: str = "", stream=None) -> str:
        prompts.append(prompt)
        return "the-secret"

    class Terminal:
        def isatty(self) -> bool:
            return True

        def readline(self) -> str:  # pragma: no cover - the assertion
            raise AssertionError("a terminal must never be read with echo on")

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    monkeypatch.setattr(sys, "stdin", Terminal())
    assert _read_secret_hidden() == "the-secret"
    assert prompts and "hidden" in prompts[0]


# ---------------------------------------------------------------- what install.sh promises


def test_every_signer_command_install_sh_documents_exists():
    text = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    documented: set[str] = set()
    for alt in re.findall(r"kaiba(?:\.cli\.main)? signer ([a-z|]+)", text):
        documented |= set(alt.split("|"))
    assert {"serve", "keygen"} <= documented, documented
    registered = set(get_command(app).commands["signer"].commands)  # type: ignore[attr-defined]
    assert documented <= registered, f"install.sh documents {sorted(documented - registered)}"
