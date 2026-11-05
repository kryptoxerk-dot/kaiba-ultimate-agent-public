"""The skill library: format, cross-references, and drift against the tool surface.

The valuable test in this file is :func:`test_every_kaiba_tool_mentioned_exists`. A skill
is a procedure the agent follows literally, so a skill that names a tool the MCP server
does not expose is a broken instruction that only fails at runtime, in a trading session,
after the model has already committed to a plan. Comparing every ``kaiba_*`` mention
against :data:`kaiba.mcp.server.TOOLS` turns that into a build failure.

The rest is the frontmatter contract from ``skills/README.md`` plus one safety assertion:
no skill may describe a capability for moving value out of an agent wallet. There is no
such tool, there is no code path in the signer that would sign one, and a skill that
implies otherwise would be teaching the agent a lie about its own limits.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from kaiba.mcp import server

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"

#: Frontmatter keys every skill must carry.
REQUIRED_KEYS = ("name", "description", "version", "author", "license", "platforms")

MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 60
MAX_BODY_LINES = 500

_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
#: Lower-case on purpose: ``KAIBA_ROOT`` and ``KAIBA_ENV`` are environment variables, not
#: tools, and must not be mistaken for one.
_TOOL_MENTION = re.compile(r"\bkaiba_[a-z][a-z0-9_]*\b")

#: Phrases that would describe moving value out of an agent wallet, or handling the
#: material that would let someone else do it. The tool names come from the server so the
#: two lists cannot drift apart.
FORBIDDEN_PHRASES: tuple[str, ...] = tuple(sorted(server.FORBIDDEN_TOOL_NAMES)) + (
    "withdraw to",
    "withdraw funds",
    "withdrawal tool",
    "send funds",
    "transfer funds",
    "move funds to",
    "sweep funds",
    "private key",
    "seed phrase",
    "export key",
)


def _skill_paths() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*/SKILL.md"))


def _skill_ids() -> list[str]:
    return [p.parent.name for p in _skill_paths()]


def _split(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter, body). Raises AssertionError on a malformed document."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: must open with a '---' frontmatter fence"
    end = text.find("\n---\n", 4)
    assert end != -1, f"{path}: frontmatter fence is not closed"
    meta = yaml.safe_load(text[4:end])
    assert isinstance(meta, dict), f"{path}: frontmatter must be a YAML mapping"
    return meta, text[end + len("\n---\n") :]


SKILL_PATHS = _skill_paths()
SKILL_IDS = _skill_ids()
ALL_NAMES = frozenset(SKILL_IDS)

pytestmark = pytest.mark.skipif(not SKILL_PATHS, reason="no skills present")


def test_skills_directory_is_populated():
    """A silently empty glob would make every parametrised test below vacuous."""
    assert SKILLS_DIR.is_dir()
    assert len(SKILL_PATHS) >= 16, f"expected the full library, found {len(SKILL_PATHS)}"
    assert (SKILLS_DIR / "README.md").is_file()


def test_no_stray_skill_directories():
    """Every directory under skills/ is a skill; a typo must not disappear silently."""
    dirs = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    assert dirs == ALL_NAMES, f"directories without a SKILL.md: {sorted(dirs - ALL_NAMES)}"


@pytest.mark.parametrize("path", SKILL_PATHS, ids=SKILL_IDS)
def test_frontmatter_parses_and_is_complete(path: Path):
    meta, _ = _split(path)
    missing = [k for k in REQUIRED_KEYS if k not in meta]
    assert not missing, f"{path}: missing frontmatter keys {missing}"
    assert _SEMVER.match(str(meta["version"])), f"{path}: version must be semver"
    assert str(meta["author"]).strip(), f"{path}: author must not be blank"
    assert str(meta["license"]).strip(), f"{path}: license must not be blank"
    platforms = meta["platforms"]
    assert isinstance(platforms, list) and platforms, f"{path}: platforms must be a list"
    assert set(platforms) <= {"linux", "macos", "windows"}, f"{path}: unknown platform"


@pytest.mark.parametrize("path", SKILL_PATHS, ids=SKILL_IDS)
def test_name_matches_directory(path: Path):
    meta, _ = _split(path)
    name = str(meta["name"])
    assert name == path.parent.name, f"{path}: name '{name}' != directory '{path.parent.name}'"
    assert len(name) <= MAX_NAME_CHARS, f"{path}: name longer than {MAX_NAME_CHARS} chars"
    assert _KEBAB.match(name), f"{path}: name must be kebab-case"


@pytest.mark.parametrize("path", SKILL_PATHS, ids=SKILL_IDS)
def test_description_is_one_short_sentence(path: Path):
    meta, _ = _split(path)
    description = str(meta["description"])
    assert len(description) <= MAX_DESCRIPTION_CHARS, (
        f"{path}: description is {len(description)} chars, limit {MAX_DESCRIPTION_CHARS}"
    )
    assert description.endswith("."), f"{path}: description must end with a period"
    assert "\n" not in description, f"{path}: description must be a single line"


@pytest.mark.parametrize("path", SKILL_PATHS, ids=SKILL_IDS)
def test_body_is_present_and_bounded(path: Path):
    _, body = _split(path)
    assert body.strip(), f"{path}: body is empty"
    lines = body.splitlines()
    assert len(lines) < MAX_BODY_LINES, (
        f"{path}: body is {len(lines)} lines, limit {MAX_BODY_LINES} — move detail into references/"
    )


@pytest.mark.parametrize("path", SKILL_PATHS, ids=SKILL_IDS)
def test_related_skills_resolve(path: Path):
    meta, _ = _split(path)
    hermes = (meta.get("metadata") or {}).get("hermes") or {}
    related = hermes.get("related_skills") or []
    assert isinstance(related, list), f"{path}: related_skills must be a list"
    unknown = [r for r in related if r not in ALL_NAMES]
    assert not unknown, f"{path}: related_skills point at non-existent skills {unknown}"
    assert path.parent.name not in related, f"{path}: a skill must not relate to itself"


def test_every_kaiba_tool_mentioned_exists():
    """The drift guard: skills may only name tools the MCP server actually exposes.

    A skill naming ``kaiba_submit_intent`` would read perfectly and fail only when the
    agent tried to call it, mid-decision. Renaming or removing a tool in
    ``kaiba/mcp/server.py`` must break this test, not a trading session.
    """
    available = set(server.TOOLS)
    offenders: dict[str, set[str]] = {}
    for path in SKILL_PATHS:
        mentioned = set(_TOOL_MENTION.findall(path.read_text(encoding="utf-8")))
        unknown = mentioned - available
        if unknown:
            offenders[path.parent.name] = unknown
    assert not offenders, (
        "skills name tools that kaiba.mcp.server.TOOLS does not expose: "
        + "; ".join(f"{k}: {sorted(v)}" for k, v in sorted(offenders.items()))
    )


def test_tool_surface_is_covered_by_the_library():
    """The other direction: a tool nothing documents is a tool nobody will use well."""
    mentioned: set[str] = set()
    for path in SKILL_PATHS:
        mentioned |= set(_TOOL_MENTION.findall(path.read_text(encoding="utf-8")))
    undocumented = set(server.TOOLS) - mentioned
    assert not undocumented, f"no skill mentions these tools: {sorted(undocumented)}"


@pytest.mark.parametrize("path", SKILL_PATHS, ids=SKILL_IDS)
def test_no_skill_mentions_a_withdrawal_capability(path: Path):
    """Withdrawal is the one hard gate; a skill may not imply the agent has it.

    The forbidden tool names come from ``server.FORBIDDEN_TOOL_NAMES`` so that adding a
    name there automatically bans it here too.
    """
    text = path.read_text(encoding="utf-8").lower()
    hits = [phrase for phrase in FORBIDDEN_PHRASES if phrase.lower() in text]
    assert not hits, f"{path}: mentions a value-exfiltration capability: {hits}"


@pytest.mark.parametrize("path", SKILL_PATHS, ids=SKILL_IDS)
def test_body_has_the_required_sections(path: Path):
    """The six-part structure from skills/README.md, checked loosely on headings."""
    _, body = _split(path)
    lowered = body.lower()
    for needle in ("what this skill is for", "when to use it", "procedure", "what not to do"):
        assert needle in lowered, f"{path}: body is missing a '{needle}' section"
    assert "source" in lowered or "config/risk.yaml" in lowered, (
        f"{path}: thresholds must cite a source"
    )
