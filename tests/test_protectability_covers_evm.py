"""Never open what you cannot monitor -- on every chain, not just Solana.

MEASURED 2026-09-23. A live robinhood position (``0x9f06f1978809b119...``, sm-trenches)
filled, went blind, stayed blind past the 300 s budget (``longest_blind_s: 585``) and
tripped ``protection_blind_timeout``, which halted entries on every chain. A second
robinhood position had been blind for 194 minutes. At that moment::

    quote_asset_is_protectable(ROBINHOOD, that_token)
        -> (False, "protection_path_has_no_price")
    engine._protection_refusal(that_decision)
        -> None          # allowed

The probe knew. The gate never asked it, because ``_protection_refusal`` opened with::

    if decision.chain is not Chain.SOL:
        return None

and justified it: "EVM already enforces the same rule through the same function:
``quote_asset_rate`` probes the quote asset before it will rate one". That is true and it
is not the same question. ``quote_asset_rate`` asks whether the **quote asset** can be
priced. For a robinhood token quoted in native ETH the quote asset is ETH, which is always
priceable, so the check passes and **nothing ever asks whether the watchdog can price the
position's own token**. On Solana the token itself was probed; on EVM it never was.

The cost of closing it is one provider call per entry, which is what this rule is worth:
a position that cannot be priced is a position whose stop cannot be evaluated, and the
failsafe's only remaining move is to halt trading on every chain -- which is exactly what
it did.
"""

from __future__ import annotations

import pytest

from kaiba.core.schemas import Chain
from kaiba.execution import engine as E


class _Decision:
    def __init__(self, chain: Chain, token: str = "0xdeadbeef") -> None:
        self.chain = chain
        self.token = token


@pytest.fixture
def unprotectable(monkeypatch):
    monkeypatch.setattr(
        E, "_protectability_probe",
        lambda chain, token: (False, "protection_path_has_no_price"),
    )


@pytest.fixture
def protectable(monkeypatch):
    monkeypatch.setattr(E, "_protectability_probe", lambda chain, token: (True, "venue"))


# ------------------------------------------------------------------ every chain is probed


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_an_unpriceable_token_is_refused_on_every_chain(chain, unprotectable):
    """THE REGRESSION: on EVM this returned None and the position opened blind."""
    refusal = E._protection_refusal(_Decision(chain))
    assert refusal, f"{chain.value} allowed an entry the watchdog cannot price"
    assert "protect" in refusal.lower() or "price" in refusal.lower(), refusal


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_a_priceable_token_is_allowed_on_every_chain(chain, protectable):
    assert E._protection_refusal(_Decision(chain)) is None


def test_the_probe_is_actually_called_for_an_evm_decision(monkeypatch):
    """Not just the verdict -- the call itself, since the old code returned before it."""
    seen: list[tuple] = []

    def probe(chain, token):
        seen.append((chain, token))
        return True, "venue"

    monkeypatch.setattr(E, "_protectability_probe", probe)
    E._protection_refusal(_Decision(Chain.ROBINHOOD, "0xabc"))
    assert seen == [(Chain.ROBINHOOD, "0xabc")], seen


# ------------------------------------------------------------------ it fails closed


@pytest.mark.parametrize("chain", [Chain.SOL, Chain.BSC, Chain.ROBINHOOD])
def test_a_raising_probe_refuses_rather_than_allows(chain, monkeypatch):
    """"We could not check" and "we cannot see it" have the same consequence."""
    def boom(chain, token):
        raise RuntimeError("provider down")

    monkeypatch.setattr(E, "_protectability_probe", boom)
    assert E._protection_refusal(_Decision(chain)), "a failed check allowed an entry"


@pytest.mark.parametrize("bad", [None, "yes", 1, object()])
def test_a_probe_that_does_not_answer_true_refuses(bad, monkeypatch):
    monkeypatch.setattr(E, "_protectability_probe", lambda chain, token: (bad, "odd"))
    assert E._protection_refusal(_Decision(Chain.ROBINHOOD))


def test_the_solana_only_guard_is_gone():
    """The line that caused this. Its return made every EVM entry unchecked."""
    import inspect

    source = inspect.getsource(E._protection_refusal)
    assert "is not Chain.SOL" not in source, (
        "the Solana-only early return is back; EVM entries are unchecked again"
    )
