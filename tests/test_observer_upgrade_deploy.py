"""The additive observer changes only its research entrypoint."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_upgrade_dropin_changes_only_execstart_and_keeps_original_research_limits():
    dropin = ROOT / "deploy/systemd/kaiba-autolearn-quotes.service.d/95-observer-upgrade.conf"
    lines = dropin.read_text().splitlines()
    assert lines == [
        "[Service]", "ExecStart=",
        "ExecStart=/usr/bin/timeout -k 5 65 /usr/bin/nice -n 19 /usr/bin/ionice -c3 "
        "%h/kaiba/.venv/bin/python -m kaiba.learning.observer_upgrade --once",
    ]
    original = (ROOT / "deploy/systemd/kaiba-autolearn-quotes.service").read_text()
    assert "python -m kaiba.learning.prospective_quotes --once" in original
    assert "MemoryMax=200M" in original and "TimeoutStartSec=75" in original
    assert "EnvironmentFile=-%h/.config/kaiba/.env" in original
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in original
    assert "GMGN_ALLOW_AUTOMATED_TRADES" not in dropin.read_text()
    assert "kaiba.execution" not in dropin.read_text()
