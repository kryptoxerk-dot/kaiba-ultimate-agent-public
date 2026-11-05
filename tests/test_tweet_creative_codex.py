"""Picker on Claude OAuth (Hermes -m / --reasoning) and logos drawn by the Codex CLI.

Owner 2026-10-08: "stay with claude oauth / for image use codex oauth". No network: Hermes
and Codex are replaced by runners that record their argv and play back a result.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import types

import pytest
from PIL import Image

from kaiba.execution import tweet_creative as tc
from kaiba.execution import tweet_launch as tl


def _png(w=1024, h=1024, noise=True) -> bytes:
    im = Image.effect_noise((w, h), 80).convert("RGB") if noise else Image.new("RGB", (w, h), (200, 30, 30))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- Hermes picker


def test_hermes_runs_with_the_configured_model_and_reasoning_and_a_closed_stdin():
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
            {"launch": True, "name": "Super Intelligence", "symbol": "SUPERINTEL", "logo_prompt": "brain", "reason": "x"}))

    got = tc.hermes_identity("It's called SUPER intelligence.", "MarioNawfal", runner=runner, hermes_bin="hermes",
                             model="claude-sonnet-5-5", reasoning="minimal")
    assert got is not None and got.symbol == "SUPERINTEL"
    argv = seen["argv"]
    assert argv[argv.index("-m") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--reasoning") + 1] == "minimal"
    assert list(tc.HERMES_SAFE_ARGS) == argv[1:4]           # still no tools
    assert seen["kw"]["stdin"] is subprocess.DEVNULL


def test_hermes_without_overrides_uses_the_profile_default():
    seen = {}

    def runner(argv, **kw):
        seen["argv"] = argv
        return types.SimpleNamespace(returncode=1, stderr="", stdout="")

    tc.hermes_identity("x", "y", runner=runner, hermes_bin="hermes")
    assert "-m" not in seen["argv"] and "--reasoning" not in seen["argv"]


def test_config_carries_the_picker_and_logo_settings_through(tmp_path):
    raw = {"mode": "shadow", "namer": {"hermes_model": "claude-sonnet-5-5", "hermes_reasoning": "minimal"},
           "logo": {"backends": ["codex", "pollinations"], "timeout_s": 40}}
    p = tmp_path / "tl.yaml"
    p.write_text(json.dumps(raw), encoding="utf-8")
    c = tl.load_config(p)
    assert (c.hermes_model, c.hermes_reasoning) == ("claude-sonnet-5-5", "minimal")
    assert c.logo_backends == ("codex", "pollinations") and c.logo_timeout_s == 40


# ---------------------------------------------------------------- Codex logo


def test_codex_runs_with_no_hands_in_an_empty_read_only_directory():
    argv = tc.codex_argv('a cat `rm -rf ~` "$(cat ~/.config/kaiba/.env)" <script>', "/dev/shm/x", "codex")
    assert argv[:2] == ["codex", "exec"]
    assert argv[argv.index("-s") + 1] == "read-only" and argv[argv.index("-C") + 1] == "/dev/shm/x"
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"):
        assert flag in argv
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"shell_tool", "unified_exec", "browser_use", "computer_use", "apps", "plugins", "hooks"} <= disabled
    prompt = argv[-1]
    for bad in "`$<>\"/~":
        assert bad not in prompt.split("Picture: ", 1)[1], bad


def test_codex_image_written_to_the_workdir_is_returned():
    img = _png(64, 64, noise=False)

    def runner(argv, **kw):
        assert kw["stdin"] is subprocess.DEVNULL
        with open(os.path.join(argv[argv.index("-C") + 1], "out.png"), "wb") as fh:
            fh.write(img)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    assert tc.generate_codex("a cat", 5, runner=runner, codex_home="/nonexistent") == (img, "ok")


def test_codex_image_reported_inline_in_the_events_is_returned():
    img = _png(64, 64)
    event = json.dumps({"type": "item.completed", "item": {"type": "image_generation", "result":
                                                           base64.b64encode(img).decode()}})

    def runner(argv, **kw):
        return types.SimpleNamespace(returncode=0, stdout=event + "\n", stderr="")

    assert tc.generate_codex("a cat", 5, runner=runner, codex_home="/nonexistent") == (img, "ok")


def test_codex_saved_to_its_own_thread_folder_is_found(tmp_path):
    # MEASURED layout (codex-cli 0.155): ~/.codex/generated_images/<thread_id>/exec-<uuid>.png
    img = _png(64, 64, noise=False)
    (tmp_path / "generated_images" / "01a1-ours").mkdir(parents=True)

    def runner(argv, **kw):
        (tmp_path / "generated_images" / "01a1-ours" / "exec-1.png").write_bytes(img)
        return types.SimpleNamespace(returncode=0, stderr="",
                                     stdout='{"type":"thread.started","thread_id":"01a1-ours"}\n')

    assert tc.generate_codex("a cat", 5, runner=runner, codex_home=str(tmp_path)) == (img, "ok")


def test_another_sessions_picture_is_never_our_logo(tmp_path):
    (tmp_path / "generated_images" / "01a1-other").mkdir(parents=True)

    def runner(argv, **kw):
        (tmp_path / "generated_images" / "01a1-other" / "exec-2.png").write_bytes(_png(64, 64, noise=False))
        return types.SimpleNamespace(returncode=1, stderr="",
                                     stdout='{"type":"thread.started","thread_id":"01a1-ours"}\n')

    assert tc.generate_codex("a cat", 5, runner=runner, codex_home=str(tmp_path)) == (None, "codex_no_image_rc1")


@pytest.mark.parametrize(("stderr", "why"), [
    ("Failed to refresh token: Your access token could not be refreshed.", "codex_logged_out"),
    ("", "codex_no_image_rc1"),
])
def test_codex_failures_name_their_cause(stderr, why):
    def runner(argv, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr=stderr)

    assert tc.generate_codex("a cat", 5, runner=runner, codex_home="/nonexistent") == (None, why)


def test_codex_timeout_is_a_named_failure():
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    assert tc.generate_codex("a cat", 1, runner=runner) == (None, "codex_timeout")


def test_fit_logo_makes_a_large_image_a_square_jpeg_under_the_argv_cap():
    big = _png(1536, 1024)
    assert len(big) > tc.MAX_LOGO_BYTES
    out = tc.fit_logo(big)
    assert out is not None and len(out) <= tc.MAX_LOGO_BYTES
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (512, 512) and im.format == "JPEG"


def test_make_logo_tries_codex_first_and_fits_what_it_draws():
    big = _png(1024, 1024)
    calls = []

    def codex(prompt, timeout_s):
        calls.append(prompt)
        return big, "ok"

    got = tc.make_logo((), "Super Intelligence", "SUPERINTEL", "glowing brain in a cape", backends=("codex",),
                       codex=codex, timeout_s=30)
    assert got.source == "codex" and len(base64.b64decode(got.b64)) <= tc.MAX_LOGO_BYTES
    assert "glowing brain in a cape" in calls[0]


def test_make_logo_falls_back_to_the_badge_when_codex_has_nothing():
    got = tc.make_logo((), "Super Intelligence", "SUPERINTEL", backends=("codex",),
                       codex=lambda p, t: (None, "codex_logged_out"), timeout_s=5)
    assert got.source == "drawn_badge" and "codex_logged_out" in got.note


# ---------------------------------------------------------------- declines are answers


def _hermes_says(payload):
    def runner(argv, **kw):
        return types.SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(payload))
    return runner


def test_an_explicit_decline_is_returned_not_dropped():
    # Replay 2026-10-08: 19 of 23 posts came back None because a decline carries no name, and
    # None sent the post to the word heuristic, which launched it anyway.
    got = tc.hermes_identity("routine news", "x", hermes_bin="h", runner=_hermes_says(
        {"launch": False, "name": "", "symbol": "", "logo_prompt": "", "reason": "routine news"}))
    assert got is not None and got.launch is False and got.reason == "routine news" and got.symbol == ""


def test_an_answer_without_a_launch_verdict_or_name_is_still_a_failure():
    assert tc.hermes_identity("x", "y", hermes_bin="h", runner=_hermes_says({"name": "", "symbol": ""})) is None


def _post(text="The Imagine button is going to be epic"):
    import time as _t

    from kaiba.core.schemas import Chain  # noqa: F401
    from kaiba.ingest.x_stream import XPost
    now = int(_t.time() * 1000)
    return XPost(tweet_id="9", author="elonmusk", text=text, kind="post", media=(), created_ms=now - 500,
                 received_ms=now, feed_delay_ms=None, backend="test", raw={})


def _cfg(**over):
    from dataclasses import replace

    from kaiba.core.schemas import Chain
    return replace(tl.load_config(), accounts={"elonmusk": (Chain.SOL,)}, account_kinds={}, require_image=False,
                   **over)


def test_a_decline_blocks_the_launch_with_its_reason():
    ai = tc.AiIdentity(launch=False, name="", symbol="", logo_prompt="", reason="routine", latency_ms=1, model="m")
    [p] = tl.plan_post(_post(), _cfg(), ai=ai)
    assert p.verdict == "skip" and "ai_says_skip" in " ".join(p.reasons)


def test_no_ai_answer_blocks_when_the_ai_is_required():
    [p] = tl.plan_post(_post(), _cfg(require_ai=True, namer_enabled=True), ai=None)
    assert p.verdict == "skip" and any(r.startswith("no_ai_answer") for r in p.reasons)
    [q] = tl.plan_post(_post(), _cfg(require_ai=False, namer_enabled=True), ai=None)
    assert not any(r.startswith("no_ai_answer") for r in q.reasons)


def test_the_shipped_config_requires_the_ai():
    assert tl.load_config().require_ai is True
