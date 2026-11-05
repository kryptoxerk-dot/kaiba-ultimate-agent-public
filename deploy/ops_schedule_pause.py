"""Pure enabled-only schedule preparation; callers own eligibility and application.

Codex temporary schedule-maintenance scope, 2026-10-19. This module performs no
filesystem, service, database or transport operations. Returned bytes confer no
authority to apply an all-job pause while exits or other required work remain.
"""
from __future__ import annotations

import copy

import yaml
from yaml.events import (
    AliasEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

MAX_BYTES = 256 * 1024
MAX_DEPTH = 32
MAX_NODES = 20_000
MAX_JOBS = 256
_STRING = "tag:yaml.org,2002:str"
_BOOL = "tag:yaml.org,2002:bool"


class PauseRefused(ValueError):
    """A fixed preparation refusal; no input values are included."""


def _require(condition, reason):
    if not condition:
        raise PauseRefused(reason)


def _parse(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "schedule_bytes_invalid_or_oversized")
    try:
        text = raw.decode("utf-8")
        depth = nodes = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            _require(not isinstance(event, AliasEvent) and getattr(event, "anchor", None) is None,
                     "schedule_anchor_or_alias")
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                depth += 1
                nodes += 1
            elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                depth -= 1
            elif isinstance(event, ScalarEvent):
                nodes += 1
            _require(depth <= MAX_DEPTH and nodes <= MAX_NODES, "schedule_structure_cap")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise PauseRefused("schedule_yaml_invalid") from exc
    _require(isinstance(root, MappingNode), "schedule_root_not_mapping")
    pending = [root]
    while pending:
        node = pending.pop()
        if isinstance(node, MappingNode):
            keys = set()
            for key, value in node.value:
                _require(isinstance(key, ScalarNode) and key.tag == _STRING, "schedule_key_not_string")
                _require(key.value not in keys, "schedule_duplicate_key")
                keys.add(key.value)
                pending.append(value)
        elif isinstance(node, SequenceNode):
            pending.extend(node.value)
    return text, root


def pause_schedule(raw: bytes) -> tuple[bytes, tuple[str, ...]]:
    """Change only explicit canonical ``jobs.*.enabled: true`` scalars to false.

    Every job must declare its flag; all already-false flags and all other bytes
    remain unchanged. Ambiguous or unsupported input is refused, never rewritten
    through a YAML dumper. Job names follow their original mapping order.
    """
    text, root = _parse(raw)
    top = {key.value: value for key, value in root.value}
    jobs = top.get("jobs")
    _require(isinstance(jobs, MappingNode) and 0 < len(jobs.value) <= MAX_JOBS, "schedule_jobs_invalid_or_overcount")
    changes, names = [], []
    for key, job in jobs.value:
        _require(0 < len(key.value) <= 256 and isinstance(job, MappingNode), "schedule_job_invalid")
        fields = {field.value: value for field, value in job.value}
        enabled = fields.get("enabled")
        _require(isinstance(enabled, ScalarNode) and enabled.tag == _BOOL and enabled.style is None,
                 "schedule_enabled_not_explicit_canonical_bool")
        start, end = enabled.start_mark.index, enabled.end_mark.index
        spelling = text[start:end]
        _require(spelling in {"true", "false"}, "schedule_enabled_not_explicit_canonical_bool")
        if spelling == "true":
            changes.append((start, end))
            names.append(key.value)
    paused = text
    for start, end in reversed(changes):
        paused = paused[:start] + "false" + paused[end:]
    result = paused.encode("utf-8")
    _parse(result)
    try:
        expected = copy.deepcopy(yaml.safe_load(text))
        for name in names:
            expected["jobs"][name]["enabled"] = False
        _require(yaml.safe_load(paused) == expected, "schedule_semantic_delta")
    except PauseRefused:
        raise
    except (yaml.YAMLError, RecursionError, TypeError, KeyError, ValueError) as exc:
        raise PauseRefused("schedule_semantic_delta") from exc
    return result, tuple(names)
