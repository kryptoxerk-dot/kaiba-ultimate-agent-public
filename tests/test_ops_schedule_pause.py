"""Byte-preserving pure schedule preparation; no applied schedule or service calls."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

from deploy import ops_schedule_pause as pause


def _restore_only_changed_flags(paused, names):
    text = paused.decode("utf-8")
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    jobs = next(value for key, value in root.value if key.value == "jobs")
    spans = []
    for key, job in jobs.value:
        if key.value in names:
            enabled = next(value for field, value in job.value if field.value == "enabled")
            spans.append((enabled.start_mark.index, enabled.end_mark.index))
    for start, end in reversed(spans):
        assert text[start:end] == "false"
        text = text[:start] + "true" + text[end:]
    return text.encode("utf-8")


def _semantic_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).digest()


def test_actual_repository_schedule_only_changes_true_job_flags_in_memory():
    path = Path(__file__).resolve().parents[1] / "config/schedule.yaml"
    raw = path.read_bytes()
    before_hash = hashlib.sha256(raw).digest()
    original = yaml.safe_load(raw)
    expected = copy.deepcopy(original)
    names = tuple(name for name, job in original["jobs"].items() if job["enabled"] is True)
    for name in names:
        expected["jobs"][name]["enabled"] = False
    result, changed = pause.pause_schedule(raw)
    assert changed == names and changed
    assert _semantic_hash(yaml.safe_load(result)) == _semantic_hash(expected)
    assert hashlib.sha256(_restore_only_changed_flags(result, changed)).digest() == before_hash
    assert hashlib.sha256(path.read_bytes()).digest() == before_hash
    assert pause.pause_schedule(result) == (result, ())


def test_utf8_crlf_comments_nested_retention_and_already_false_bytes_are_preserved():
    raw = ("# 智能 é 🐈 enabled: true\r\n"
        "retention: {enabled: true, swaps_days: 14}\r\n"
        "jobs:\r\n"
        "  α:\r\n"
        "    enabled: true # keep comment\r\n"
        "    params: {enabled: true, note: 'enabled: true'}\r\n"
        "  beta: {enabled: false, interval_s: 60}\r\n"
        "  γ: {enabled: true}\r\n").encode()
    expected = raw.replace(b"enabled: true # keep", b"enabled: false # keep").replace(
        "γ: {enabled: true}".encode(), "γ: {enabled: false}".encode())
    result, names = pause.pause_schedule(raw)
    assert result == expected and names == ("α", "γ")
    assert _restore_only_changed_flags(result, names) == raw
    assert pause.pause_schedule(result) == (result, ())


def test_all_false_is_byte_identical_including_utf8_bom_and_no_final_newline():
    raw = b"\xef\xbb\xbfjobs: {a: {enabled: false}} # untouched"
    assert pause.pause_schedule(raw) == (raw, ())


@pytest.mark.parametrize("raw", [
    b"jobs: {a: {enabled: true, enabled: false}}",
    b"jobs: {a: {enabled: true}, a: {enabled: false}}",
    b"jobs: {a: {enabled: true}}\njobs: {b: {enabled: false}}",
    b"jobs: {a: {enabled: true, params: {x: 1, x: 2}}}",
    b"retention: {x: 1, x: 2}\njobs: {a: {enabled: true}}",
])
def test_duplicate_keys_are_rejected_even_outside_changed_job_flags(raw):
    with pytest.raises(pause.PauseRefused, match="schedule_duplicate_key"):
        pause.pause_schedule(raw)


@pytest.mark.parametrize("raw", [
    b"jobs: &jobs {a: {enabled: true}}",
    b"other: &x true\njobs: {a: {enabled: *x}}",
    b"base: &x {enabled: true}\njobs: {a: {<<: *x}}",
    b"other: &x [1,2]\njobs: {a: {enabled: true}}",
])
def test_anchors_aliases_and_merge_ambiguity_are_refused(raw):
    with pytest.raises(pause.PauseRefused, match="schedule_anchor_or_alias"):
        pause.pause_schedule(raw)


@pytest.mark.parametrize("value", ["True", "FALSE", "yes", "off", "0", "null", "'true'", '"false"',
    "!!bool true", "!!bool 'false'", "[true]", "{value: true}"])
def test_enabled_requires_an_explicit_plain_canonical_boolean(value):
    with pytest.raises(pause.PauseRefused, match="schedule_enabled_not_explicit_canonical_bool"):
        pause.pause_schedule(("jobs: {a: {enabled: " + value + "}}").encode())


@pytest.mark.parametrize("raw", [
    b"{}", b"jobs: {}", b"jobs: null", b"jobs: [a]", b"jobs: {a: false}",
    b"jobs: {a: {interval_s: 60}}", b"[jobs]", b"jobs: {true: {enabled: true}}",
    b"jobs: {a: {enabled: true}}\nextra: {? [a, b]: 1}",
])
def test_missing_or_unsupported_explicit_job_structure_is_refused(raw):
    with pytest.raises(pause.PauseRefused):
        pause.pause_schedule(raw)


@pytest.mark.parametrize("raw", [
    b"jobs: {a: {enabled: true}}\n---\njobs: {b: {enabled: true}}",
    b"jobs: [", b"jobs: \xff", b"jobs: \x00", b"", "jobs: {a: {enabled: true}}", bytearray(b"jobs: {}"),
])
def test_invalid_yaml_encoding_multidocument_and_nonbyte_inputs_are_refused(raw):
    with pytest.raises(pause.PauseRefused):
        pause.pause_schedule(raw)


def test_byte_depth_node_and_job_caps_refuse_without_truncation():
    cases = [
        b" " * (pause.MAX_BYTES + 1),
        ("jobs: {a: {enabled: true, params: " + "[" * (pause.MAX_DEPTH + 1)
            + "0" + "]" * (pause.MAX_DEPTH + 1) + "}}").encode(),
        ("jobs: {a: {enabled: true}}\nother: [" + ",".join("0" for _ in range(pause.MAX_NODES)) + "]").encode(),
        ("jobs:\n" + "".join(f"  job{i}: {{enabled: true}}\n" for i in range(pause.MAX_JOBS + 1))).encode(),
    ]
    for raw in cases:
        with pytest.raises(pause.PauseRefused):
            pause.pause_schedule(raw)


@pytest.mark.parametrize("scalar", ["2026-99-01", "9" * 5000], ids=["invalid-timestamp", "oversized-integer"])
def test_scalar_construction_value_errors_use_the_closed_refusal_contract(scalar):
    raw = ("jobs: {a: {enabled: true}}\nextra: " + scalar + "\n").encode()
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(4300)
        with pytest.raises(pause.PauseRefused, match="^schedule_semantic_delta$"):
            pause.pause_schedule(raw)
    finally:
        sys.set_int_max_str_digits(previous)
