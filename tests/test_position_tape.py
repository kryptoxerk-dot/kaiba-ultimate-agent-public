"""Parity with original tape/evaluator, bounded memory and fail-closed capture."""
from __future__ import annotations

import dataclasses
import gc
import hashlib
import json
import os
import tracemalloc
from pathlib import Path

import pytest

from kaiba.learning import position_tape as pt
from kaiba.learning import shadow_tape
from kaiba.learning import twin_exits as twin

T0 = 1_800_000_000_000
DAY = 86_400_000


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("socket.socket.connect", lambda *a, **k: pytest.fail("network forbidden"))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("transport forbidden"))


def row(pid="selected", obs=T0, **extra):
    return {"pid": pid, "obs": obs, "upd": obs + 7, "chain": "sol", "token": "synthetic",
        "px": "1.0000", "xq": None, "liq": "1000000", "lk": "pool", "src": "fixture",
        "venue": "pumpswap", "pool": "fixture-pool", "basis": "verified_onchain",
        "settle": {"nested": [None, "keep", 1.25]}, **extra}


def write_rows(directory, values, *, day=None, newline=True):
    directory.mkdir(parents=True, exist_ok=True)
    if day is None:
        day = pt._days(T0, T0)[1]
    path = directory / (day + ".jsonl")
    path.write_bytes(("\n".join(json.dumps(r, separators=(",", ":")) for r in values)
                      + ("\n" if newline else "")).encode())
    return path


def kwargs(tmp_path):
    return {"spool_root": tmp_path}


def load_copy(tape):
    return {pid: list(rows) for pid, rows in tape}


def test_golden_old_loader_cross_day_windows_duplicates_ties_and_all_fields(tmp_path):
    source = tmp_path / "tape"
    start, end = T0 - DAY, T0 + DAY
    windows = [pt.Window("selected", T0 - 20, T0 + 10), pt.Window("other", start, end),
               pt.Window("empty", start, end)]
    days = pt._days(start, end)
    # Deliberately place equal-time rows on different days: old enumeration order is authoritative.
    write_rows(source, [row(obs=T0 + 5, marker="a"), row(obs=T0 - 21), row(obs=start - 1),
                        row("other", end)], day=days[0])
    middle = write_rows(source, [row(obs=T0, marker="first"), row(obs=T0 + 5, marker="b"),
        row(obs=T0 + 5, marker="b"), row(obs=T0 + 60_010), row(obs=T0 + 60_011),
        row("unselected", T0), row("other", end + 1)], day=days[2])
    with middle.open("ab") as stream:
        stream.write(b'{bad complete line}\n\n')
        stream.write(b'{"oversized_integer":' + b'9' * 5000 + b'}\n')
        stream.write((json.dumps(row(None)) + '\n').encode())
    write_rows(source, [row(obs=T0 + 5, marker="c")], day=days[-1], newline=False)
    original = shadow_tape.load(source, start, end)
    expected = {w.pid: [r for r in original.get(w.pid, []) if w.opened <= r["obs"] <= w.closed + 60_000]
                for w in windows}
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    with pt.PositionTape(source, start, end, windows, **kwargs(tmp_path)) as tape:
        directory = tape.directory
        assert load_copy(tape) == expected
        assert tape.receipt["malformed_complete_lines"] == 3
        assert tape.receipt["selected_rows"] == sum(map(len, expected.values()))
        assert "not a complete end-time snapshot" in tape.receipt["scope"]
    assert not directory.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before


def test_iteration_lends_one_list_and_clears_it_before_next_position(tmp_path):
    source = tmp_path / "tape"
    write_rows(source, [row("a"), row("b")])
    with pt.PositionTape(source, T0, T0 + 1, [pt.Window(p, T0, T0) for p in ("a", "b")], **kwargs(tmp_path)) as tape:
        iterator = iter(tape)
        _, first = next(iterator)
        assert len(first) == 1
        _, second = next(iterator)
        assert first == [] and len(second) == 1
        with pytest.raises(StopIteration):
            next(iterator)
        assert second == []
        with pytest.raises(pt.TapeError, match="single_iteration"):
            next(iter(tape))


@pytest.mark.parametrize("fault,expected", [
    ("line", "line_byte_cap"), ("source_bytes", "source_byte_cap"),
    ("source_rows", "source_row_cap"), ("position_rows", "position_row_cap"),
    ("position_bytes", "position_byte_cap"), ("spool", "spool_byte_cap"),
    ("decoded", "decoded_position_cap"), ("partial", "partial_prefix_record"),
    ("shape", "invalid_record_shape"), ("utf8", "invalid_record_encoding")])
def test_caps_and_truncated_or_invalid_records_refuse_and_clean(tmp_path, fault, expected):
    source = tmp_path / "tape"
    values = [row("a"), row("a"), row("b"), row("b")]
    path = write_rows(source, values)
    changes = {"line": {"max_line_bytes": 20}, "source_bytes": {"max_source_bytes": 10},
        "source_rows": {"max_source_rows": 1}, "position_rows": {"max_position_rows": 1},
        "position_bytes": {"max_position_bytes": 10},
        "spool": {"max_spool_bytes": 900, "max_position_bytes": 900},
        "decoded": {"max_decoded_bytes": 100}}
    if fault == "partial":
        with path.open("ab") as stream:
            stream.write(b'{"pid":"unfinished"')
    elif fault == "shape":
        path.write_bytes(b'{}\n')
    elif fault == "utf8":
        path.write_bytes(b'\xff\n')
    before = path.read_bytes()
    limits = dataclasses.replace(pt.Limits(), **changes.get(fault, {}))
    with pytest.raises(pt.TapeError, match=expected):
        with pt.PositionTape(source, T0, T0 + 1, [pt.Window(p, T0, T0) for p in ("a", "b")],
                             limits=limits, **kwargs(tmp_path)) as tape:
            list(tape)
    assert not list(tmp_path.glob(pt.PREFIX + "*"))
    assert path.read_bytes() == before


def test_append_after_prefix_capture_is_excluded_and_disclosed(tmp_path, monkeypatch):
    source = tmp_path / "tape"
    path = write_rows(source, [row(marker="captured")])
    before, original, appended = path.read_bytes(), pt._decode, False

    def append(raw):
        nonlocal appended
        if not appended:
            appended = True
            with path.open("ab") as stream:
                stream.write((json.dumps(row(marker="later")) + "\n").encode())
        return original(raw)

    monkeypatch.setattr(pt, "_decode", append)
    with pt.PositionTape(source, T0, T0 + 1, [pt.Window("selected", T0, T0)], **kwargs(tmp_path)) as tape:
        assert load_copy(tape)["selected"] == [row(marker="captured")]
        [receipt] = tape.receipt["source_files"]
        assert receipt["captured_prefix_bytes"] == len(before)
        assert receipt["prefix_sha256"] == hashlib.sha256(before).hexdigest()
        assert receipt["appended_bytes_observed"] > 0
        assert tape.receipt["observed_growing_file_count"] == 1
        assert tape.receipt["missing_day_count"] == 2
        assert tape.receipt["captured_file_count"] == 1
    assert b'later' in path.read_bytes()


@pytest.mark.parametrize("fault", ["rewrite", "truncate", "replace"])
def test_source_mutation_cannot_publish_partial_selection(tmp_path, monkeypatch, fault):
    if fault == "replace" and os.name == "nt":
        pytest.skip("Windows refuses replacing an open file; exercised on POSIX")
    source = tmp_path / "tape"
    path = write_rows(source, [row(), row()])
    original, changed = pt._decode, False

    def mutate(raw):
        nonlocal changed
        if not changed:
            changed = True
            if fault == "rewrite":
                path.write_bytes(path.read_bytes().replace(b'1.0000', b'2.0000'))
            elif fault == "truncate":
                path.write_bytes(b'')
            else:
                replacement = source / "replacement"
                replacement.write_bytes(path.read_bytes())
                os.replace(replacement, path)
        return original(raw)

    monkeypatch.setattr(pt, "_decode", mutate)
    with pytest.raises(pt.TapeError, match="source_prefix|source_replaced"):
        with pt.PositionTape(source, T0, T0 + 1, [pt.Window("selected", T0, T0)], **kwargs(tmp_path)):
            pytest.fail("Incomplete capture must never reach evaluation")
    assert not list(tmp_path.glob(pt.PREFIX + "*"))


def test_spool_tamper_refuses_but_context_cleans_owned_files(tmp_path):
    source = tmp_path / "tape"
    write_rows(source, [row()])
    with pt.PositionTape(source, T0, T0 + 1, [pt.Window("selected", T0, T0)], **kwargs(tmp_path)) as tape:
        path = next(tape.directory.iterdir())
        path.write_bytes(path.read_bytes().replace(b'1.0000', b'2.0000'))
        with pytest.raises(pt.TapeError, match="spool_changed"):
            list(tape)
    assert not list(tmp_path.glob(pt.PREFIX + "*"))


def test_limits_windows_and_duplicate_positions_are_explicit(tmp_path):
    source = tmp_path / "tape"
    for windows, options, reason in (
        ([pt.Window("x", T0, T0)] * 2, {}, "duplicate_position"),
        ([pt.Window("x", T0 + 1, T0)], {}, "duplicate_position"),
        ([], {"tail_ms": 0}, "60s"),
        ([], {"limits": dataclasses.replace(pt.Limits(), max_spool_bytes=pt.HARD_SPOOL_BYTES + 1)}, "invalid_limit"),
    ):
        with pytest.raises(pt.TapeError, match=reason):
            pt.PositionTape(source, T0, T0 + 1, windows, **kwargs(tmp_path), **options)
    with pytest.raises(pt.TapeError, match="position_cap"):
        pt.PositionTape(source, T0, T0 + 1, [pt.Window(str(i), T0, T0) for i in range(2)],
                         limits=dataclasses.replace(pt.Limits(), max_positions=1), **kwargs(tmp_path))
    with pytest.raises(pt.TapeError, match="day_cap"):
        pt.PositionTape(source, T0, T0 + 33 * DAY, [], **kwargs(tmp_path))


def test_symlink_input_or_spool_root_is_refused(tmp_path):
    source = tmp_path / "tape"
    real = write_rows(source, [row()])
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create test symlinks")
    with pytest.raises(pt.TapeError, match="symlink"):
        pt.PositionTape(linked, T0, T0 + 1, [], **kwargs(tmp_path))
    with pytest.raises(pt.TapeError, match="symlink"):
        pt.PositionTape(source, T0, T0 + 1, [], spool_root=linked)
    saved = tmp_path / "original"
    real.rename(saved)
    real.symlink_to(saved)
    with pytest.raises(pt.TapeError, match="symlink"):
        with pt.PositionTape(source, T0, T0 + 1, [], **kwargs(tmp_path)):
            pass
    assert saved.exists()


def test_cleanup_never_recurses_into_unowned_content(tmp_path):
    source = tmp_path / "tape"
    write_rows(source, [row()])
    tape = pt.PositionTape(source, T0, T0 + 1, [pt.Window("selected", T0, T0)], **kwargs(tmp_path))
    tape.__enter__()
    foreign = tape.directory / "foreign"
    foreign.write_bytes(b"keep")
    with pytest.raises(pt.TapeError, match="unexpected_spool_file"):
        tape.close()
    assert foreign.read_bytes() == b"keep"
    foreign.unlink()
    tape.close()


def test_partial_unlink_failure_can_retry_only_remaining_owned_files(tmp_path, monkeypatch):
    source = tmp_path / "tape"
    write_rows(source, [row("a"), row("b")])
    tape = pt.PositionTape(source, T0, T0 + 1, [pt.Window(p, T0, T0) for p in ("a", "b")], **kwargs(tmp_path))
    tape.__enter__()
    original, failed = Path.unlink, False

    def unlink(path, *args, **kw):
        nonlocal failed
        if path.name == "00001.jsonl" and not failed:
            failed = True
            raise OSError("synthetic unlink failure")
        return original(path, *args, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="synthetic unlink failure"):
        tape.close()
    assert [p.name for p in tape.directory.iterdir()] == ["00001.jsonl"]
    tape.close()
    assert not list(tmp_path.glob(pt.PREFIX + "*"))


@pytest.mark.parametrize("point", ["capture", "context_close"])
def test_flush_failure_closes_every_handle_and_cleans_owned_spool(tmp_path, monkeypatch, point):
    source = tmp_path / "tape"
    write_rows(source, [row("a"), row("b")])
    original_append = pt.PositionTape._append
    real_handles = []

    class BrokenClose:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def close(self):
            self.handle.close()
            raise OSError("synthetic flush failure")

    def append(self, index, raw):
        original_append(self, index, raw)
        if index == 1:
            real_handles.extend(self._handles.values())
            self._handles[0] = BrokenClose(self._handles[0])

    if point == "capture":
        monkeypatch.setattr(pt.PositionTape, "_append", append)
        with pytest.raises(OSError, match="synthetic flush failure"):
            with pt.PositionTape(source, T0, T0 + 1, [pt.Window(p, T0, T0) for p in ("a", "b")],
                                 **kwargs(tmp_path)):
                pytest.fail("No partial capture")
    else:
        tape = pt.PositionTape(source, T0, T0 + 1, [pt.Window("a", T0, T0)], **kwargs(tmp_path))
        tape.__enter__()
        real_handles = [next(tape.directory.iterdir()).open("ab") for _ in range(2)]
        tape._handles[0], tape._handles[1] = BrokenClose(real_handles[0]), real_handles[1]
        with pytest.raises(OSError, match="synthetic flush failure"):
            tape.close()
    assert all(handle.closed for handle in real_handles)
    assert not list(tmp_path.glob(pt.PREFIX + "*"))


def test_memory_scales_with_one_position_not_entire_tape(tmp_path):
    source = tmp_path / "tape"
    source.mkdir()
    path = source / (pt._days(T0, T0)[1] + ".jsonl")

    def measure(count):
        with path.open("wb") as stream:
            for index in range(count):
                for sample in range(160):
                    stream.write((json.dumps(row(str(index), T0 + sample, padding="x" * 512)) + "\n").encode())
        windows = [pt.Window(str(i), T0, T0 + 200) for i in range(count)]
        gc.collect()
        tracemalloc.start()
        try:
            with pt.PositionTape(source, T0, T0 + 200, windows, **kwargs(tmp_path)) as tape:
                for _, rows in tape:
                    assert len(rows) == 160
                receipt = tape.receipt
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        return peak, receipt

    small, _ = measure(2)
    large, receipt = measure(100)
    assert receipt["selected_rows"] == 16_000 and receipt["spool_bytes"] > 12_000_000
    assert receipt["max_loaded_position_rows"] == 160
    assert large < 6_000_000 and large < small + 2_000_000


def test_twin_exits_reads_one_position_at_a_time_and_keeps_the_last_report_on_failure(tmp_path, monkeypatch):
    # Was a review-only patch of the source; the wiring is real since 2026-11-04.
    positions = [twin.Pos("selected", "sm-trenches", "sol", T0, T0 + 600_000,
        1_000_000_000, 1_000_000, 900_000_000, 1.0, "stop_loss", [twin.Leg(T0 + 600_000, 1_000_000)]),
        twin.Pos("no-tape", "sm-trenches", "sol", T0, T0 + 100_000, 1, 1, 1, None, None)]
    tape_dir = tmp_path / "tape"
    write_rows(tape_dir, [row(obs=T0 + i * 12_000, px=str(1 + (i % 9) / 10)) for i in range(51)])
    monkeypatch.setattr(twin, "_spool_root", lambda: tmp_path)
    monkeypatch.setattr(twin, "load_positions", lambda *a: positions)
    monkeypatch.setattr(twin.NativeUsd, "at", lambda *a: 200)
    monkeypatch.setattr(twin.time_exit_trial, "load_trials", lambda: {})
    now = T0 + 1_300_000

    def forbid(*args, **kwargs):
        pytest.fail("twin_exits must not materialize the whole tape")

    monkeypatch.setattr(shadow_tape, "load", forbid)
    actual = twin.run_once(tmp_path / "never-opened.db", tape_dir, tmp_path / "new", days=1, now_ms=now)
    assert actual["positions"] == 2 and actual["positions_without_tape"] == 1
    assert actual["tape_provenance"]["selected_rows"] == 51
    assert json.loads((tmp_path / "new/latest.json").read_bytes())["rows"] == actual["rows"]
    published = (tmp_path / "new/latest.json").read_bytes()
    original_load = pt.PositionTape._load

    def fail_after_one(self, index):
        if index == 1:
            raise pt.TapeError("position_tape:decoded_position_cap")
        return original_load(self, index)

    monkeypatch.setattr(pt.PositionTape, "_load", fail_after_one)
    with pytest.raises(pt.TapeError, match="decoded_position_cap"):
        twin.run_once(tmp_path / "never-opened.db", tape_dir, tmp_path / "new", days=1, now_ms=now)
    assert (tmp_path / "new/latest.json").read_bytes() == published
    assert not list(tmp_path.glob(pt.PREFIX + "*"))
