"""The paper observer is a separate bounded research process with provider access."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_observer_unit_limits_resources_and_preserves_existing_paper_sandbox():
    unit = (ROOT / 'deploy/systemd/kaiba-autolearn-quotes.service').read_text()
    assert 'Type=oneshot' in unit
    assert 'python -m kaiba.learning.prospective_quotes --once' in unit
    assert '/usr/bin/timeout -k 5 65 /usr/bin/nice -n 19 /usr/bin/ionice -c3' in unit
    assert 'TimeoutStartSec=75' in unit and 'MemoryMax=200M' in unit
    assert 'NoNewPrivileges=true' in unit and 'PrivateTmp=true' in unit
    assert 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' in unit
    assert 'ConditionPathExists=%h/kaiba/data/learning/autolearn/state.json' in unit
    paper = (ROOT / 'deploy/systemd/kaiba-autolearn.service').read_text()
    assert 'RestrictAddressFamilies=AF_UNIX\n' in paper
    assert 'GMGN_ALLOW_AUTOMATED_TRADES' not in unit
    assert 'kaiba.execution' not in unit


def test_observer_timer_uses_existing_paper_research_and_restarts_after_completion():
    timer = (ROOT / 'deploy/systemd/kaiba-autolearn-quotes.timer').read_text()
    assert 'Unit=kaiba-autolearn-quotes.service' in timer
    assert 'OnUnitInactiveSec=10s' in timer
    assert 'AccuracySec=2s' in timer
    assert 'WantedBy=timers.target' in timer
