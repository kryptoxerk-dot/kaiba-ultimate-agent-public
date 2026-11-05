import copy

import pytest

from deploy.activate_provider_economy import UNITS, require_quiet, validate_packet


@pytest.fixture
def snapshot():
    return {"orders_in_flight": 0, "live_open_positions": 0,
            "configs": {"risk": "original"}, "sources": {"http": "committed"}}


@pytest.mark.parametrize("field,value", [("orders_in_flight", 1), ("live_open_positions", 1),
                                        ("orders_in_flight", "deferred_wal")])
def test_reload_refuses_live_unresolved_or_unavailable_snapshot(snapshot, field, value):
    changed = {**snapshot, field: value}
    with pytest.raises(ValueError, match="inventory_reload_refused"):
        require_quiet(changed, {"lanes": {"one": {"mode": "shadow"}}}, snapshot)
    require_quiet(snapshot, {"lanes": {"one": {"mode": "shadow"}}}, snapshot)


@pytest.mark.parametrize("mode", ["live", "canary", None])
def test_reload_refuses_nonpaper_lane_even_without_inventory(snapshot, mode):
    with pytest.raises(ValueError, match="nonpaper_lane"):
        require_quiet(snapshot, {"lanes": {"one": {"mode": mode}}}, snapshot)


def test_reload_refuses_config_drift_and_never_owns_hermes(snapshot):
    changed = copy.deepcopy(snapshot)
    changed["configs"]["risk"] = "changed"
    with pytest.raises(ValueError, match="configuration_drift"):
        require_quiet(changed, {"lanes": {"one": {"mode": "shadow"}}}, snapshot)
    assert all(unit.startswith("kaiba-") and "hermes" not in unit for unit in UNITS)


def test_resume_requires_positive_pid_of_an_owned_worker():
    packet = {"configs": {}, "sources": {}, "already_reloaded": {"kaiba-ops.service": "42"}}
    assert validate_packet(packet) == {"kaiba-ops.service": "42"}
    packet["already_reloaded"] = {"hermes-gateway-kaiba-operator.service": "42"}
    with pytest.raises(ValueError, match="verified_reload_inventory_invalid"):
        validate_packet(packet)
