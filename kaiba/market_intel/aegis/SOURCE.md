# Source: Aegis Omni market-intel collectors (vendored, content-identical)

- **Origin.** `C:/CLAUDE/AEGIS OMNI/apps/aegis_terminal/intel/` at commit `4f61245` (the owner's local
  Aegis Omni repository, never pushed), copied 2026-11-03.
- **Claim.** MARKET-INTEL-REFRESH-CLAUDE-20261103 (lead), for the owner's unified-dashboard request.
- **Not edited, except the deviations below.** Each file is the origin's committed content with line endings normalized to LF, as
  this repository's `.gitattributes` stores every text file. That content is also identical to the
  origin's working copy after the same normalization.
  - `tests/test_market_intel_refresh.py` pins the hashes below.
  - The three B905 lint findings are ignored for this directory in `pyproject.toml`, not edited.
- **Evidence for the parsers.** Aegis's own offline suite (`apps/aegis_terminal/intel/test_*.py`,
  recorded fixtures, no network) passed against this content in the Aegis repo on 2026-11-03:
  `pytest -q apps/aegis_terminal/intel` gave **67 passed in 0.80 s**. Those tests import
  `apps.aegis_terminal.intel...` and are not copied here.
- **Not copied:**
  - `networth.py` (the owner's portfolio);
  - `social.py` (Telegram/Reddit/X; X needs a paid token);
  - `registry.py`, the terminal server and the fixtures.

| file (LF) | sha256 |
|---|---|
| `__init__.py` | `9cf965492e6dabcf693e07bb8743132b9894fbbc257a3a9e7d105ded382d9b67` |
| `base.py` | `6041232eed3bc3e6d6c47e26a55287046300f917e196f79d42787f02f75688aa` |
| `econ_calendar.py` | `c287f8a707ab27f126d9dadd80a57aac5454f8e58bcdcca570cd2a50d44f473e` |
| `macro.py` | `51de71d1151f90fa6501dc3cada10a485aaeddb8c934433877600e5ba0004c0e` |
| `institutions.py` | `2f197e0b2172476c3f46c4bfa4dd8c5ed86788b1e632faa1785b280fdc6db567` |
| `news.py` | `1feea1d8ffee91e689ff0f3cb1c6688cf89ab223ecb886db646b01346c883bf7` |
| `whales.py` | `1cd71dcc9209bf116278d54d696d3dd37edbcf632d611ea5385fca5c7be0604b` |
| `market.py` | `ea8c310a16905a50731f7fcb47b2c21b809a6015292b5bc341d06abc08afeb9c` |

## Deviations from the origin (each recorded, tested and pinned)

1. **`news.py`: future-dated headlines are quarantined, never re-dated** (2026-11-03,
   MARKET-INTEL-NEWS-CLOCK-CLAUDE-20261103, found by Codex's review of 8e85fb5).
   - **What Aegis did.** `merge` rewrote any item dated more than 10 minutes ahead of the fetch to
     `ts=now, published=now`. That fabricated its publication clock, so a future-dated Fed or CPI
     headline would read as current news and count toward `high_24h`.
   - **What it does now.** `split_future` keeps such items, with their ORIGINAL `ts`/`published`, in a
     separate `future_dated` list (at most 50, plus `future_dated_count`). They are never re-dated,
     never archived and never counted. `merge` leaves them out.
   - **Hashes.** Origin content `e315428700556791f8cc821e60354c11a60c97176ecf91095cca61b30a5303f9`; this repository `1feea1d8ffee91e689ff0f3cb1c6688cf89ab223ecb886db646b01346c883bf7`.
   - **Witness.** `tests/test_market_intel_refresh.py`
     (`test_a_future_dated_headline_keeps_its_clock_and_stays_out_of_current_news`).
