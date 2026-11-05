"""J7Tracker's social feed, recorded read-only for Hermes (owner request 2026-10-28).

WHY. The owner asked to "implement https://j7tracker.io/ to kaiba ultimate agent to check
tweets" (board row J7-FEED-20261028). J7 pushes the posts of the accounts it tracks (X, plus
Truth Social, Instagram, TikTok, YouTube and Binance Square) over Socket.IO, each with the
contract address J7 detected in it. Nothing in Kaiba kept that feed: the tweet launcher's
bridge (:mod:`kaiba.ingest.j7_launch_bridge`) takes only what the launcher needs and stores
nothing. This listener records the feed so the MCP read tool ``kaiba_tweets`` can answer
"check tweets".

WHAT IT RECORDS, in its OWN SQLite file ``<data dir>/j7_feed.db`` (never ``kaiba.db``):

* ``posts``: one row per platform and post id. Author (handle lower-cased, id, display
  name), kind (``j7_launch_bridge.KINDS``), text (at most 4,000 characters), the post's own
  time, our first-seen / last-update / deletion times, ``initial=1`` for posts first seen in
  the ``initialTweets`` backlog a connection opens with (a forward study must exclude
  them), quoted and reply ids, media count, bounded metrics and the first contract address.
* ``mentions``: one row per post x chain x address J7 attached (``contractAddress``,
  ``extraContractAddresses``, or a ``token_meta`` naming that post), with the time that
  address first appeared on that post (a later ``tweet_update`` can add one). Addresses
  pass ``looks_solana`` / ``looks_evm``; EVM is lower-cased; J7's chain string becomes a
  Kaiba chain where that is unambiguous and stays raw otherwise. J7's chain and address
  are upstream hints, not chain evidence.
* ``token_meta``: J7's symbol, name, market cap, liquidity and honeypot flag per address
  (an exact repeat within 60 s is dropped).
* ``j7_events``: follow, profile, follow-scan, charity and upstream-status events, with a
  JSON payload of at most 8 KB.
* ``counters``: events this module does not know, counted by name and never stored.
* ``feed_state``: the session token's ``exp`` (when it is a JWT), for ``kaiba_tweets``.

Every later copy of a post merges the way ``j7_launch_bridge.merge_nonempty`` does: a newer
non-empty value wins, an empty one never blanks a stored value, and a reply, quote or repost
never turns back into a plain post (the bridge ORs ``isReply`` / ``isQuote`` / ``isRetweet``).
That includes a second ``tweet`` for a known id: J7 says custom accounts "can deliver it twice
(fast and full versions)", and keeping only the first kept the fast copy without its media,
quote or metrics. A deletion keeps the row and stamps ``deleted_ms``. Every string is cleaned
before it is stored (:func:`strip_invisible`): control and format characters (terminal
escapes, zero-width and bidi characters, the TAG block that spells hidden text) are dropped
and a lone UTF-16 surrogate becomes U+FFFD, so neither a hidden instruction nor an
unencodable string reaches the file. A row SQLite still refuses drops alone
(``dropped:row``); the rest of its batch is kept.

SIZE. Rows older than 14 days are pruned every hour in batches of at most 5,000, and freed
pages go back to the disk (the file is created ``auto_vacuum=INCREMENTAL``); while the feed
is off or cannot run, every idle return prunes an existing file the same way (the runner
looks again every 6 h), so switching the feed off does not freeze 14 days on the disk. Past 512 MiB of
used pages the store stops keeping text and payloads (ids, authors, times and every mention
still go in), retention drops to 7 days, and the status row says ``store_capacity``. Past
1.25 x that, or with the disk holding the data directory at 88 % (kaiba-disk-guard runs
emergency retention at 85 % and halts entries at 92 %), it stores nothing but counters and
says ``store_full``. A store that refuses writes altogether (a ``-wal`` left by a manual run
as root, a damaged file) says ``store_error``. Each pages once (kaiba/ops/trade_notify.py).

WHAT IT NEVER DOES. It never trades, never creates or launches a token, never places an
order and never writes to J7: no account change, no wallet material, none of J7's trading
endpoints (those take wallet signing keys). The one client-to-server message it sends is the
``user_connected`` handshake J7 requires after every connect; the Socket.IO engine answers
the server's pings by itself.

THE SESSION TOKEN. J7 authenticates the feed with the owner's login session (a JWT, not a
wallet key). Only the owner turns the feed on, by writing it as ONE line to
``~/.config/kaiba/j7.session`` (mode 600, owned by the user that runs kaiba-ingest) over
SSH; :func:`file_session_token` reads that file and nothing else, and refuses one that other
accounts can read (``insecure_j7_session``) or that it cannot read (``unreadable_j7_session``).
It never travels through Telegram, Hermes, Discord or a support
ticket. ``J7_SESSION_JWT`` is refused, not read: the launcher bridge's reader prefers that
variable to the file, so with it set, deleting the file (the off switch) and refreshing it
would both change nothing, and every child process kaiba-ingest starts (gmgn-cli) would
inherit the login. While it is set :func:`run` returns ``{"idle": "j7_session_in_env"}``,
which pages. Without a token :func:`run` returns ``{"idle": "missing_j7_session"}`` without
touching the network, and the runner's idle recheck picks a token written later up without
a restart. Deleting the file turns the feed off (within 30 s while it runs). A file that is
there but unusable (:data:`FILE_FAULTS`: empty, as `cat >` leaves it for the length of a
paste, not one line, wrong owner or mode, unreadable) never turns a RUNNING feed off: the
session in use is kept for :data:`TOKEN_FILE_GRACE_MS`, then the listener goes ``down`` with
the code (a page) and re-reads the file every 30 s. The token never
reaches a log line, an exception, a status row, the store or a return value. When J7 refuses
it -- one of its documented refusals, or any other refusal of the namespace connect
(``auth_error:refused``) -- the listener goes ``down`` and waits 900 s before it tries the
SAME token again (doubling to at most 6 h; "too many connections" stays at 900 s), and
reconnects within 30 s of the file changing. When the token is a JWT
its ``exp`` claim (and nothing else) is read locally: INFO logs the lifetime once per token,
the status row carries ``token_expiring:<exp>`` from an hour before (a page), and past
``exp`` the listener does not connect at all (``auth_error:token_expired``) until the file
holds a new token. How long J7's tokens live is NOT measured yet.
docs/runbooks/j7-feed.md is the owner's page.

Post text is untrusted third-party content. It is stored and returned as data, never
logged, never followed as an instruction, and it is not a trading signal until a measured
study says so.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import importlib.util
import json
import logging
import math
import os
import random
import re
import shutil
import sqlite3
import stat
import sys
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kaiba.core.redact import REDACTED, redact_text
from kaiba.core.schemas import Chain, looks_evm, looks_solana, now_ms
from kaiba.ingest import j7_launch_bridge
from kaiba.ingest.j7_launch_bridge import (
    HOSTS,
    KINDS,
    SIO_USER_CONNECTED,
    FeedUnavailable,
    timestamp_ms,
)

log = logging.getLogger(__name__)

#: ``ingest_status.feed`` and the runner's registry name.
FEED = "j7_feed"
STORE_NAME = "j7_feed.db"
REGION_ENV = "J7_FEED_REGION"
DEFAULT_REGION = "nyc"
#: The variable the launcher bridge reads BEFORE the token file. j7_feed refuses to run while
#: it is set (:func:`file_session_token`); it never reads its value.
TOKEN_ENV = "J7_SESSION_JWT"

# ---- connection ---------------------------------------------------------------------------
#: python-socketio's ``wait_timeout``: it bounds only the wait for the namespace CONNECT.
CONNECT_TIMEOUT_S = 15
#: The hard deadline on the whole ``client.connect()``, which also gives way to ``stop``.
#: python-engineio reads the OPEN packet with a bare ``await ws.receive()`` and aiohttp's
#: upgrade waits up to 300 s, so a J7 node that accepts the upgrade and then stalls would
#: otherwise hold the listener (and a SIGTERM'd kaiba-ingest) inside connect() indefinitely.
CONNECT_DEADLINE_S = CONNECT_TIMEOUT_S + 15
#: Reconnect after a network failure or a dropped socket: 5 s doubling to 300 s, +0-25 %.
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 300.0
BACKOFF_JITTER = 0.25
#: After J7 refuses the session: the same token is not tried again for 900 s, doubling per
#: consecutive refusal to at most 6 h. J7's own advice is "log in again, don't retry with the
#: same token" (docs/errors); a changed token is tried at once. "Too many connections" (the
#: owner's browser tabs count) retries every 900 s without doubling: the token is fine.
AUTH_BACKOFF_S = 900.0
AUTH_BACKOFF_CAP_S = 6 * 3600.0
#: A session that lasted this long resets the reconnect backoff, and a drop after it retries
#: the same region first (a node restart). A shorter one is a failure, posts or not: the
#: backoff grows and the next connect goes to the other region. Until 2026-10-28 one post made
#: a session healthy and only a failed connect or a stall switched regions, so a node that
#: accepted the socket and dropped it was redialled every 5 s, or retried on the same host for
#: ever, while J7 says both hosts carry the same feed (protocol review r3).
HEALTHY_SESSION_MS = 60_000
#: Connected but no live post for this long: reconnect once, on the other region, before the
#: 30-minute silence page (kaiba/ops/trade_notify.py). Skipped while J7 reports its own
#: upstream X feed disconnected, which a reconnect cannot fix.
STALL_MS = 15 * 60_000
#: How often the token is re-read (a local file read, never the network).
TOKEN_RECHECK_MS = 30_000
#: J7: "Invalid token" means the JWT "is expired or isn't an access token. Log in again" --
#: Kaiba cannot refresh it (that would need J7's account endpoints). So the status row says
#: ``token_expiring:<exp s>`` (and the owner is paged) this long before a JWT's ``exp``, and
#: past ``exp`` the listener does not connect until the file holds a new token.
TOKEN_WARN_MS = 3_600_000
#: A usable token is one line of printable ASCII, at most this long.
TOKEN_MAX_CHARS = 16_384
#: Bytes read from the token file: TOKEN_MAX_CHARS of ASCII plus a BOM and a line end. A
#: larger file is not a token, and it is never read whole (it is re-read every 30 s).
TOKEN_READ_BYTES = TOKEN_MAX_CHARS + 16
#: The token file is THERE but holds no usable token: empty or not one line
#: (``invalid_j7_session``), readable by other accounts or owned by another user
#: (``insecure_j7_session``), or unreadable, such as a root-owned file
#: (``unreadable_j7_session``). Each pages. While the listener runs, none of them ends the
#: run as ``missing_j7_session`` (the owner's off switch) does: the session in use is kept for
#: :data:`TOKEN_FILE_GRACE_MS`, then the listener goes ``down`` with the code and re-reads the
#: file every :data:`TOKEN_RECHECK_MS`, so the owner's fix is picked up within 30 s.
FILE_FAULTS: frozenset[str] = frozenset({"invalid_j7_session", "insecure_j7_session", "unreadable_j7_session"})
#: How long the token file may read as a FILE_FAULT, on every re-read, before that ends a
#: session or a refusal wait. `cat > j7.session` truncates the file before the paste, so a
#: refresh done that way leaves it empty for as long as the paste takes; one 30 s re-read in
#: that window ended the run and the runner parked the feed for 6 h (protocol review r3,
#: measured: a 20 s paste reconnected 21,587 s after the file was whole again). Two minutes
#: also covers a `chmod 600` typed after the write.
TOKEN_FILE_GRACE_MS = 120_000
#: ``last_error`` codes that mean "no usable token" (``kaiba_tweets`` shows no expiry for them).
NO_TOKEN_CODES: tuple[str, ...] = ("missing_j7_session", "j7_session_in_env", *sorted(FILE_FAULTS))
TICK_S = 1.0
WAIT_STEP_S = 5.0
CLOSE_TIMEOUT_S = 5.0

# ---- health -------------------------------------------------------------------------------
#: ``ingest_status`` is written at most this often (the final write on shutdown excepted),
#: and only when something changed, so a ``down`` row ages into the pager's 300 s grace.
STATUS_EVERY_MS = 60_000

# ---- store --------------------------------------------------------------------------------
BATCH_ROWS = 200
BATCH_MS = 1_000
RETENTION_MS = 14 * 86_400_000
PRUNE_EVERY_MS = 3_600_000
PRUNE_BATCH = 5_000
#: Used pages (not file size: freed pages are reused) past which text and payloads stop.
CAP_BYTES = 512 * 1024 * 1024
#: Text resumes once retention has brought the used pages back under this share of the cap.
CAP_RESUME = 0.9
#: The hard ceiling ("store_full"): past CAP_BYTES x this, or with the disk that holds the
#: data directory at DISK_FULL_FRACTION used (df's Use%), nothing but counters and the
#: token's expiry is stored. It lifts under 90 % of the ceiling and DISK_RESUME_FRACTION.
#: The box disk read 86 % on 2026-10-28; kaiba-disk-guard runs emergency retention at 85 %
#: and halts entries at 92 %, and it does not know this file.
FULL_FACTOR = 1.25
DISK_FULL_FRACTION = 0.88
DISK_RESUME_FRACTION = 0.86
#: Retention while at the cap (or full): 7 days instead of 14, so the cap clears sooner.
CAPACITY_RETENTION_MS = 7 * 86_400_000
#: After a prune, freed pages go back to the disk this many at a time (auto_vacuum=INCREMENTAL).
VACUUM_STEP_PAGES = 2_048
VACUUM_MAX_STEPS = 64
CAPACITY_CHECK_MS = 60_000
WAL_LIMIT_BYTES = 64 * 1024 * 1024
TEXT_CHARS = 4_000
#: Post text is cut to TEXT_CHARS + this BEFORE it is cleaned and scrubbed (the scrub's cost
#: grows with the input; a secret that starts before TEXT_CHARS still fits whole).
SCRUB_WINDOW = 4_096
PAYLOAD_BYTES = 8 * 1024

# ---- memory bounds ------------------------------------------------------------------------
INITIAL_MAX_ITEMS = 200
RECENT_IDS = 5_000
META_SEEN = 5_000
TOKEN_META_REPEAT_MS = 60_000
#: J7: a ``token_meta`` "can arrive before the item, so buffer it for a few seconds".
PENDING_LINKS = 1_000
PENDING_LINK_MS = 15_000
COUNTER_NAMES = 64

# ---- read API -----------------------------------------------------------------------------
READ_DEADLINE_S = 5.0
READ_MAX_ROWS = 50
READ_MAX_MINUTES = RETENTION_MS // 60_000

#: J7's documented events (docs.j7tracker.io/docs/feed-events, read 2026-10-28), by what
#: this module does with them. Anything else is counted by name and dropped.
SIDE_EVENTS: frozenset[str] = frozenset({
    "following_update", "unfollowing_update", "affiliated_update", "unaffiliated_update",
    "profile_update", "profile_affiliation_update", "profile_pinned_update",
    "profile_unpinned_update", "profile_suspended_update", "profile_deactivated_update",
    "follow_scan", "charity_event",
})
#: ``quoted_tweet`` repeats what the parent carries as ``quotedTweet``;
#: ``hidden_accounts_updated`` is the owner's own settings; ``pong`` answers a ping we never
#: send. Counted, not stored.
COUNTED_EVENTS: frozenset[str] = frozenset({"quoted_tweet", "hidden_accounts_updated", "pong"})
RECORDED_EVENTS: frozenset[str] = frozenset({
    "initialTweets", "tweet", "tweet_update", "tweet_deleted", "external_message", "token_meta",
    "status",
}) | SIDE_EVENTS
#: ``external_message`` patches; only the YouTube deletion changes a stored row.
PATCH_TYPES: frozenset[str] = frozenset({"TIKTOK_VIDEO_UPDATE", "YOUTUBE_IMAGE_UPDATE", "YOUTUBE_DELETION"})

#: J7's refusal messages (docs/errors) -> the stable ``last_error`` code. ``connect_error``
#: carries the first three (and the fourth) at the handshake, ``auth_error`` the last two
#: after ``user_connected``. Each one means "log in again" or "close a socket"; retrying the
#: same token fixes none of them, so all are a refusal here.
AUTH_CODES: dict[str, str] = {
    "invalid token": "auth_error:invalid_token",
    "account disabled": "auth_error:account_disabled",
    "unknown user": "auth_error:unknown_user",
    "too many connections": "auth_error:too_many_connections",
    "invalid session": "auth_error:invalid_session",
}
#: Any OTHER refusal of the namespace connect. A server-side CONNECT_ERROR reaches
#: python-socketio's ``connect_error`` handler as the mapping ``{"message": ...}``; a
#: transport failure (DNS, TLS, a refused upgrade) arrives there as a plain string instead.
#: An undocumented message ("jwt expired", a plan or subscription refusal) is still J7
#: refusing THIS token, so it takes the refusal backoff, not the network one: retried on the
#: network schedule, "Subscription expired" reconnected every 300 s, ~288 times a day
#: (protocol review, 2026-10-28), against J7's "don't retry with the same token".
REFUSED_CODE = "auth_error:refused"

#: J7 chain strings that name exactly one Kaiba chain. J7 documents no value list ("chain"
#: is a plain string), so anything else is kept raw rather than guessed.
CHAIN_ALIASES: dict[str, str] = {
    "sol": Chain.SOL.value,
    "solana": Chain.SOL.value,
    "bsc": Chain.BSC.value,
    "bnb": Chain.BSC.value,
    "bnbchain": Chain.BSC.value,
    "bnb chain": Chain.BSC.value,
    "binance smart chain": Chain.BSC.value,
    "eth": Chain.ETH.value,
    "ethereum": Chain.ETH.value,
    "base": Chain.BASE.value,
    "robinhood": Chain.ROBINHOOD.value,
    "robinhood chain": Chain.ROBINHOOD.value,
}
#: The chain of an EVM-shaped address J7 gave no (usable) chain for.
EVM_FAMILY = "evm"

_ID_SHAPE = re.compile(r"^[A-Za-z0-9_:.\-]{1,128}$")
_AUTHOR_ID = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")
_METRIC_KEY = re.compile(r"^[a-z_]{1,24}$")
_HANDLE = re.compile(r"^[a-z0-9_.:\-]{1,64}$")
_CHAIN_HINT = re.compile(r"^[a-z0-9 _.\-]{1,32}$")
_COUNTER = re.compile(r"[^A-Za-z0-9_.:\-]+")
_PAIR = re.compile(r"^[A-Za-z0-9_\-/.$]{1,24}$")
_TOKEN_SHAPE = re.compile(r"[\x21-\x7e]+")
_ASCII_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
#: A JWT-shaped run, the fallback behind the exact-token scrub. Linear on any input: the run
#: must start a word (the look-behind) and the quantifiers are possessive, so "eyJeyJeyJ..."
#: costs one pass instead of a quadratic backtrack (4.5 s on 100,000 characters before).
_JWT_SHAPE = re.compile(
    r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{4,}+\.[A-Za-z0-9_\-]{4,}+\.[A-Za-z0-9_\-]{4,}+")
_PLATFORM_FLAGS: tuple[tuple[str, str], ...] = (
    ("isTruthSocial", "truth_social"),
    ("isInstagram", "instagram"),
    ("isTikTok", "tiktok"),
    ("isYouTube", "youtube"),
    ("isBinanceSquare", "binance_square"),
)


# ------------------------------------------------------------------------------- secrets


class Secrets:
    """The session tokens this process has read, so they can be scrubbed out of everything
    written or logged. Bounded: the last four (a refreshed token joins, the old one stays)."""

    def __init__(self) -> None:
        self._values: list[str] = []

    def add(self, value: str) -> None:
        if value and value not in self._values:
            self._values.append(value)
            del self._values[:-4]

    def scrub(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        return _JWT_SHAPE.sub(REDACTED, text)


class _ScrubFilter(logging.Filter):
    """Scrubs the session token out of records on this module's and the Socket.IO loggers.

    Logger filters run before propagation, so this reaches every handler. The rendered
    message is stored back so a token in an argument cannot reappear at format time, and a
    traceback is rendered (and scrubbed) here because no later layer knows the token.
    """

    def __init__(self, secrets: Secrets) -> None:
        super().__init__()
        self.secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            clean = redact_text(self.secrets.scrub(message), secrets=())
            if clean != message:
                record.msg = clean
                record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = self.secrets.scrub(record.exc_text)
        except Exception:  # noqa: BLE001 - logging must never be what fails
            pass
        return True


_FILTERED_LOGGERS: tuple[str, ...] = (__name__, "socketio", "socketio.client", "engineio", "engineio.client")


def _install_filter(secrets: Secrets) -> _ScrubFilter:
    filt = _ScrubFilter(secrets)
    for name in _FILTERED_LOGGERS:
        logging.getLogger(name).addFilter(filt)
    return filt


def _remove_filter(filt: _ScrubFilter) -> None:
    for name in _FILTERED_LOGGERS:
        logging.getLogger(name).removeFilter(filt)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def usable_token(value: Any) -> bool:
    """One line of printable ASCII without whitespace, at most :data:`TOKEN_MAX_CHARS`: what
    an auth token is. Anything else is ``invalid_j7_session`` and is never sent anywhere."""
    return isinstance(value, str) and len(value) <= TOKEN_MAX_CHARS and bool(_TOKEN_SHAPE.fullmatch(value))


def _current_uid() -> int | None:
    """This process's uid, or ``None`` where there is none (Windows: no owner or mode check)."""
    getuid = getattr(os, "getuid", None)
    return getuid() if callable(getuid) else None


def _read_token_file(target: Path) -> tuple[bytes, int | None, int]:
    """``(at most TOKEN_READ_BYTES + 1 bytes, owner uid or None, permission bits)`` of the
    token file, the stat and the read from ONE open descriptor (the file cannot be swapped
    between them). Non-blocking open, so a FIFO at the path cannot hang the ingest loop; a
    directory, FIFO or device is an ``OSError``."""
    fd = os.open(target, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        chunks: list[bytes] = []
        remaining = TOKEN_READ_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks), getattr(info, "st_uid", None), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def file_session_token(path: Path | None = None) -> str:
    """The owner's J7 session token, from ``~/.config/kaiba/j7.session`` and nowhere else.

    Re-read on every call (the listener does so every :data:`TOKEN_RECHECK_MS`), so deleting
    or refreshing the file takes effect without a restart. The file branch of
    :func:`kaiba.ingest.j7_launch_bridge.session_token`, with its validation, minus the
    variable it reads first: with :data:`TOKEN_ENV` set that reader ignores the file, which
    turned the owner's off switch and every refresh into silent no-ops (security review,
    2026-10-28). A set variable therefore raises ``j7_session_in_env`` (its value is never
    read).

    The other codes (2026-10-28, review r3), each a stable ``FeedUnavailable``:

    * ``missing_j7_session``: no such file, or no such directory on the way. Only this is
      the owner's off switch, which never pages. Every other ``OSError`` used to read as it
      too, so a root-owned file (written with sudo) turned a switched-on feed silently off.
    * ``unreadable_j7_session``: any other ``OSError`` (permission denied, a directory).
    * ``insecure_j7_session`` (POSIX): the file is not owned by this process's user, or any
      group or other permission bit is set. The token is the owner's J7 login (it is a
      Bearer credential for J7's account API too), so a file other accounts can read is
      never used. The repo's older J7 enable script refuses the same file for the same reason.
    * ``invalid_j7_session``: empty, more than one word, not UTF-8, or larger than any token.
    """
    if os.environ.get(TOKEN_ENV, "").strip():
        raise FeedUnavailable("j7_session_in_env")
    target = Path(path) if path is not None else Path(j7_launch_bridge.TOKEN_FILE)
    try:
        raw, owner, mode = _read_token_file(target)
    except (FileNotFoundError, NotADirectoryError):
        raise FeedUnavailable("missing_j7_session") from None
    except OSError:
        raise FeedUnavailable("unreadable_j7_session") from None
    me = _current_uid()
    if me is not None and (mode & 0o077 or owner != me):
        raise FeedUnavailable("insecure_j7_session")
    if len(raw) > TOKEN_READ_BYTES:
        raise FeedUnavailable("invalid_j7_session")
    try:
        value = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:  # not UTF-8: not a token
        raise FeedUnavailable("invalid_j7_session") from None
    if not value or any(c.isspace() for c in value):
        raise FeedUnavailable("invalid_j7_session")
    return value


def token_expiry_ms(token: str) -> int | None:
    """The ``exp`` claim of a JWT-shaped token in epoch ms, or ``None``.

    Read locally: the middle segment is base64url-decoded, nothing is verified and nothing
    leaves the process. Only the integer is kept; no other claim is returned, logged or
    stored. ``None`` for an opaque token, a token without a sane ``exp`` (2017..2100) or
    anything that does not decode.
    """
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3 or not parts[1] or len(parts[1]) > 4_096:
        return None
    segment = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, TypeError, RecursionError):  # binascii.Error and JSON/Unicode errors are ValueErrors
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    del claims
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        return None
    ms = int(exp * 1000)
    return ms if 1_500_000_000_000 <= ms <= 4_102_444_800_000 else None


def _utc(ms: int | None) -> str:
    if ms is None:
        return "?"
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _code(exc: BaseException) -> str:
    """A stable idle code from a FeedUnavailable; never its text if that is not a code."""
    value = str(exc)
    return value if re.fullmatch(r"[a-z0-9_:]{1,64}", value) else "invalid_j7_session"


_STABLE_CODE = re.compile(r"^[a-z0-9_:.\-]{1,64}$")


def _stable(code: str | None) -> str | None:
    """``last_error`` holds stable codes only (``auth_error:invalid_session``,
    ``connect_failed:nyc``); anything else, such as provider text that could carry the token,
    becomes ``unrecognized_error`` before it can reach the status row."""
    if not code:
        return None
    return code if _STABLE_CODE.fullmatch(code) else "unrecognized_error"


# --------------------------------------------------------------------------- normalizers


def strip_invisible(text: str) -> str:
    """``text`` without the characters a reader cannot see but a model reads, and without
    the ones SQLite cannot store.

    A lone UTF-16 surrogate (a JavaScript slice through an emoji survives JSON decoding as
    one, and binding it raises ``UnicodeEncodeError``) becomes U+FFFD. Dropped: control
    characters other than ``\\n`` and ``\\t`` (ESC, so no ANSI/OSC terminal sequence; CR;
    BEL; C1), every format character (zero-width spaces and joiners, bidi overrides and
    isolates, U+FEFF, the TAG block U+E0000-E007F that spells invisible ASCII), private-use
    and unassigned code points, and variation selectors 17-256 (U+E0100-E01EF, another
    hidden byte channel). Everything else, emoji included, is kept.
    """
    if text.isascii():
        return _ASCII_CONTROL.sub("", text)
    out: list[str] = []
    for ch in text:
        if ch == "\n" or ch == "\t":
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        if category == "Cs":
            out.append("\ufffd")
        elif category[0] != "C" and not "\U000e0100" <= ch <= "\U000e01ef":
            out.append(ch)
    return "".join(out)


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = strip_invisible(value[: limit + 64]).strip()  # cut first: the cost is per character
    return value[:limit] or None


def post_id_of(value: Any) -> str | None:
    """A post id (X digits, ``bsq-``/``yt-``/``wh-`` ids), or ``None`` when it is not one."""
    if isinstance(value, Mapping):
        value = value.get("id")
    text = _text(value, 128)
    return text if text and _ID_SHAPE.fullmatch(text) else None


def normalize_handle(value: Any) -> str | None:
    """``@ElonMusk`` -> ``elonmusk``; ``None`` for anything that is not a handle."""
    text = _text(value, 80)
    if text is None:
        return None
    text = text.lstrip("@").lower()
    return text if _HANDLE.fullmatch(text) else None


def platform_of(payload: Mapping[str, Any]) -> str:
    """``x`` for J7's own tweets; the social platform for its ``SocialPost``s; ``webhook``."""
    if (payload.get("isWebhook") or payload.get("source") == "webhook"
            or str(payload.get("type") or "").upper() == "WEBHOOK"):
        return "webhook"
    raw = payload.get("platform")
    if isinstance(raw, str) and raw.strip():
        name = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")[:24]
        if name:
            return name
    for flag, name in _PLATFORM_FLAGS:
        if payload.get(flag):
            return name
    return "external" if payload.get("source") == "external" else "x"


def kind_of(payload: Mapping[str, Any], platform: str) -> str | None:
    """post / reply / quote / repost (the launcher bridge's precedence), webhook, or None."""
    label = str(payload.get("type") or "").upper()
    kind = KINDS.get(label)
    if payload.get("isRetweet") is True:
        kind = "repost"
    elif payload.get("isReply") is True:
        kind = "reply"
    elif payload.get("isQuote") is True:
        kind = "quote"
    if kind is None:
        if label == "WEBHOOK" or platform == "webhook":
            kind = "webhook"
        elif label == "EXTERNAL" or platform != "x":
            kind = "post"
    return kind


def created_ms_of(payload: Mapping[str, Any]) -> int | None:
    for key in ("createdAt", "timestamp", "created_at"):
        value = timestamp_ms(payload.get(key))
        if value:
            if value < 100_000_000_000:  # epoch seconds
                value *= 1000
            return value if value < 4_102_444_800_000 else None
    return None


def media_count_of(payload: Mapping[str, Any]) -> int:
    count = 0
    media = payload.get("media")
    if isinstance(media, Mapping):
        for key in ("images", "videos"):
            items = media.get(key)
            if isinstance(items, list):
                count += len(items)
    declared = payload.get("mediaCount")
    if isinstance(declared, int) and not isinstance(declared, bool) and 0 <= declared <= 1000:
        count = max(count, declared)
    return min(count, 1000)


def metrics_of(payload: Mapping[str, Any]) -> str | None:
    """At most ten finite non-negative counters as compact JSON (X and Binance Square names).

    A key must be ``^[a-z_]{1,24}$`` (J7's are ``likes``, ``quotes``, ``replies``,
    ``retweets``, ``views``): a key is third-party text too, and "ignore previous" is not a
    metric."""
    metrics = payload.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    out: dict[str, int | float] = {}
    for key, value in list(metrics.items())[:16]:
        if (isinstance(key, str) and _METRIC_KEY.fullmatch(key) and isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(value) and value >= 0):
            out[key] = int(value) if float(value).is_integer() else round(float(value), 4)
        if len(out) >= 10:
            break
    return json.dumps(out, separators=(",", ":"), sort_keys=True) if out else None


def coin_pairs_of(payload: Mapping[str, Any]) -> str | None:
    """Binance Square's ``coinPairs`` as ``BTCUSDT,ETHUSDT`` (at most eight)."""
    pairs = payload.get("coinPairs")
    if not isinstance(pairs, list):
        return None
    found: list[str] = []
    for item in pairs[:8]:
        if isinstance(item, Mapping):
            item = item.get("symbol") or item.get("pair") or item.get("name")
        value = _text(item, 24)
        if value and _PAIR.fullmatch(value):
            found.append(value.upper())
    return ",".join(dict.fromkeys(found)) or None


def ref_of(value: Any) -> str | None:
    """The id of a quoted or replied-to post (a ``NestedTweet`` or a ``TweetRef``)."""
    return post_id_of(value.get("id")) if isinstance(value, Mapping) else post_id_of(value)


def _chain_hint(value: Any) -> str | None:
    text = _text(value, 32)
    if text is None:
        return None
    text = text.lower()
    return text if _CHAIN_HINT.fullmatch(text) else None


def _collapse(hint: str) -> str:
    return re.sub(r"[\s_\-]+", " ", hint).strip()


def resolve_address(address: Any, chain_hint: Any = None) -> tuple[str, str, str | None] | None:
    """``(chain, address, chain_hint)`` for one address J7 attached, or ``None``.

    The address shape decides the family: base58 is Solana, ``0x`` + 40 hex is EVM
    (lower-cased). J7's chain string then names the chain where :data:`CHAIN_ALIASES` maps it
    unambiguously and it agrees with the shape; an unknown string is kept as it is; an EVM
    address with no usable string is ``evm``. The raw string is always kept as the hint.
    """
    value = _text(address, 64)
    if value is None:
        return None
    if looks_evm(value):
        value, family = value.lower(), EVM_FAMILY
    elif looks_solana(value):
        family = Chain.SOL.value
    else:
        return None
    hint = _chain_hint(chain_hint)
    mapped = CHAIN_ALIASES.get(_collapse(hint)) if hint else None
    if family == Chain.SOL.value:
        chain = Chain.SOL.value if hint is None or mapped is not None else hint
    elif mapped is not None and mapped != Chain.SOL.value:
        chain = mapped
    elif mapped is None and hint is not None:
        chain = hint
    else:
        chain = EVM_FAMILY
    return chain, value, hint


def contract_hints(payload: Mapping[str, Any]) -> list[tuple[str, Any]]:
    """``(address, chain_hint)`` from a post's ``contractAddress`` and ``extraContractAddresses``."""
    hint = payload.get("chain")
    extras = payload.get("extraContractAddresses")
    found: list[tuple[str, Any]] = []
    for value in [payload.get("contractAddress"), *(extras[:8] if isinstance(extras, list) else [])]:
        if isinstance(value, str) and value.strip():
            found.append((value, hint))
    return found


def _usd(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return round(number, 2) if math.isfinite(number) and number >= 0 else None


def _flag(value: Any) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _shrink(value: Any, strings: int, items: int, depth: int) -> Any:
    if isinstance(value, str):
        return strip_invisible(value[:strings])
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= 4:
        return "..."
    if isinstance(value, Mapping):
        return {strip_invisible(str(k)[:64]): _shrink(v, strings, items, depth + 1)
                for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_shrink(v, strings, items, depth + 1) for v in value[:items]]
    return strip_invisible(str(value)[:strings])


def bounded_json(value: Any, limit: int = PAYLOAD_BYTES) -> str:
    """ASCII JSON of ``value`` in at most ``limit`` bytes: strings, lists and depth are cut
    harder until it fits, and the last resort keeps only the top-level key names. ASCII
    (``\\uXXXX`` escapes) so the size check and the store can never meet an unencodable
    character."""
    for strings, items in ((512, 20), (128, 8), (48, 3)):
        blob = json.dumps(_shrink(value, strings, items, 0), ensure_ascii=True,
                          separators=(",", ":"), default=str)
        if len(blob) <= limit:
            return blob
    keys = sorted(strip_invisible(str(k)[:40]) for k in value)[:40] if isinstance(value, Mapping) else []
    return json.dumps({"_truncated": True, "keys": keys}, separators=(",", ":"))


# ---------------------------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY,
    platform      TEXT    NOT NULL,
    post_id       TEXT    NOT NULL,
    author_handle TEXT,
    author_id     TEXT,
    author_name   TEXT,
    kind          TEXT,
    text          TEXT,
    created_ms    INTEGER,
    first_seen_ms INTEGER NOT NULL,
    updated_ms    INTEGER NOT NULL,
    deleted_ms    INTEGER,
    initial       INTEGER NOT NULL DEFAULT 0,
    quoted_id     TEXT,
    reply_to_id   TEXT,
    media_count   INTEGER NOT NULL DEFAULT 0,
    metrics       TEXT,
    coin_pairs    TEXT,
    ca            TEXT,
    ca_chain      TEXT,
    UNIQUE (platform, post_id)
);
CREATE INDEX IF NOT EXISTS posts_by_seen ON posts (first_seen_ms);
CREATE INDEX IF NOT EXISTS posts_by_author ON posts (author_handle, first_seen_ms);

CREATE TABLE IF NOT EXISTS mentions (
    id            INTEGER PRIMARY KEY,
    platform      TEXT    NOT NULL,
    post_id       TEXT    NOT NULL,
    chain         TEXT    NOT NULL,
    address       TEXT    NOT NULL,
    chain_hint    TEXT,
    source        TEXT    NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    initial       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (platform, post_id, chain, address)
);
CREATE INDEX IF NOT EXISTS mentions_by_address ON mentions (address, first_seen_ms);
CREATE INDEX IF NOT EXISTS mentions_by_seen ON mentions (first_seen_ms);

CREATE TABLE IF NOT EXISTS token_meta (
    id            INTEGER PRIMARY KEY,
    chain         TEXT    NOT NULL,
    address       TEXT    NOT NULL,
    post_id       TEXT,
    symbol        TEXT,
    name          TEXT,
    market        TEXT,
    mcap_usd      REAL,
    liquidity_usd REAL,
    is_honeypot   INTEGER,
    observed_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS token_meta_by_address ON token_meta (address, observed_ms);
CREATE INDEX IF NOT EXISTS token_meta_by_seen ON token_meta (observed_ms);

CREATE TABLE IF NOT EXISTS j7_events (
    id          INTEGER PRIMARY KEY,
    event       TEXT    NOT NULL,
    handle      TEXT,
    observed_ms INTEGER NOT NULL,
    payload     TEXT
);
CREATE INDEX IF NOT EXISTS j7_events_by_seen ON j7_events (observed_ms);
CREATE INDEX IF NOT EXISTS j7_events_by_event ON j7_events (event, observed_ms);

CREATE TABLE IF NOT EXISTS counters (
    name    TEXT    PRIMARY KEY,
    n       INTEGER NOT NULL,
    last_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_state (
    key        TEXT    PRIMARY KEY,
    value      INTEGER,
    updated_ms INTEGER NOT NULL
);
"""

_ROW_COLUMNS: tuple[str, ...] = (
    "platform", "post_id", "author_handle", "author_id", "author_name", "kind", "text",
    "created_ms", "first_seen_ms", "updated_ms", "deleted_ms", "initial", "quoted_id",
    "reply_to_id", "media_count", "metrics", "coin_pairs", "ca", "ca_chain",
)
_ROW_INSERT = (f"INSERT INTO posts ({', '.join(_ROW_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in _ROW_COLUMNS)})")
#: Every copy of a post (``tweet``, a second ``tweet`` for a known id, ``tweet_update``, the
#: backlog, social posts, deletions) merges as ``j7_launch_bridge.merge_nonempty`` does: newer
#: non-empty wins and nothing is blanked. J7's feed-events page says to drop a ``tweet`` whose
#: id is known, in the same sentence that says custom accounts "can deliver it twice (fast and
#: full versions)", while its own client example upserts every ``tweet``; dropping kept the
#: fast copy and lost the full one's media, quote and metrics (protocol review, 2026-10-28).
#: ``first_seen_ms`` and ``initial`` keep their first values; the first address stays first;
#: a reply, quote or repost never reverts to ``post`` (the bridge ORs the three flags, so a
#: fast copy that came second cannot undo what the full copy resolved).
SQL_MERGE = _ROW_INSERT + """ ON CONFLICT(platform, post_id) DO UPDATE SET
    author_handle = COALESCE(excluded.author_handle, posts.author_handle),
    author_id     = COALESCE(excluded.author_id, posts.author_id),
    author_name   = COALESCE(excluded.author_name, posts.author_name),
    kind          = CASE WHEN excluded.kind IS NULL
                              OR (excluded.kind = 'post' AND posts.kind IN ('reply', 'quote', 'repost'))
                         THEN posts.kind ELSE excluded.kind END,
    text          = COALESCE(excluded.text, posts.text),
    created_ms    = COALESCE(excluded.created_ms, posts.created_ms),
    updated_ms    = excluded.updated_ms,
    deleted_ms    = COALESCE(posts.deleted_ms, excluded.deleted_ms),
    quoted_id     = COALESCE(excluded.quoted_id, posts.quoted_id),
    reply_to_id   = COALESCE(excluded.reply_to_id, posts.reply_to_id),
    media_count   = CASE WHEN excluded.media_count > 0 THEN excluded.media_count ELSE posts.media_count END,
    metrics       = COALESCE(excluded.metrics, posts.metrics),
    coin_pairs    = COALESCE(excluded.coin_pairs, posts.coin_pairs),
    ca            = COALESCE(posts.ca, excluded.ca),
    ca_chain      = CASE WHEN posts.ca IS NULL THEN excluded.ca_chain ELSE posts.ca_chain END"""
SQL_SET_CA = "UPDATE posts SET ca = ?, ca_chain = ? WHERE platform = ? AND post_id = ? AND ca IS NULL"
SQL_MARK_DELETED = ("UPDATE posts SET deleted_ms = COALESCE(deleted_ms, ?), updated_ms = ? "
                     "WHERE platform = ? AND post_id = ?")
SQL_MENTION = ("INSERT OR IGNORE INTO mentions (platform, post_id, chain, address, chain_hint, source, "
                  "first_seen_ms, initial) VALUES (?,?,?,?,?,?,?,?)")
SQL_TOKEN_META = ("INSERT INTO token_meta (chain, address, post_id, symbol, name, market, mcap_usd, "
                     "liquidity_usd, is_honeypot, observed_ms) VALUES (?,?,?,?,?,?,?,?,?,?)")
SQL_EVENT = "INSERT INTO j7_events (event, handle, observed_ms, payload) VALUES (?,?,?,?)"
SQL_COUNTER = ("INSERT INTO counters (name, n, last_ms) VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "n = counters.n + excluded.n, last_ms = excluded.last_ms")
SQL_STATE = ("INSERT INTO feed_state (key, value, updated_ms) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
             "value = excluded.value, updated_ms = excluded.updated_ms")
#: What a full store still writes: counters (what was dropped) and the token's expiry.
_ALWAYS_WRITTEN: frozenset[str] = frozenset({SQL_COUNTER, SQL_STATE})
#: ``(table, time column)`` pruned by retention.
PRUNED: tuple[tuple[str, str], ...] = (
    ("posts", "first_seen_ms"), ("mentions", "first_seen_ms"),
    ("token_meta", "observed_ms"), ("j7_events", "observed_ms"),
)


def default_store_path() -> Path:
    """``<KAIBA_DATA_DIR>/j7_feed.db`` (the repo's ``data/`` by default)."""
    from kaiba.core.config import get_settings

    return Path(get_settings().kaiba_data_dir) / STORE_NAME


def disk_fraction(path: Path) -> float | None:
    """df's Use% of the disk that holds ``path`` (used over used + available), as a fraction;
    the same reading kaiba-disk-guard acts on. ``None`` when it cannot be read."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    denominator = usage.used + usage.free
    return usage.used / denominator if denominator > 0 else None


def _percent(fraction: float | None) -> str:
    return "?" if fraction is None else f"{fraction * 100:.0f} %"


def _environmental(exc: BaseException) -> bool:
    """A write error no row-by-row retry can get past: a lock, a full disk, an I/O error, a
    damaged file. Anything else (a lone surrogate, an integer past 64 bits, a constraint) is
    one row's fault."""
    return isinstance(exc, (sqlite3.OperationalError, MemoryError)) or type(exc) is sqlite3.DatabaseError


class Store:
    """The recorder's own SQLite file: WAL, short batched transactions, retention, a cap.

    Writes queue in memory and commit together after :data:`BATCH_MS` or at
    :data:`BATCH_ROWS`, in one ``BEGIN IMMEDIATE`` transaction each. A batch SQLite refuses
    is retried row by row (one SAVEPOINT per row), so a bad row drops alone and is counted as
    ``dropped:row``; a batch lost to the environment (a lock, a full disk) is dropped and
    counted. The connection is never left inside a transaction, and :meth:`flush` never raises.
    """

    def __init__(self, path: Path, *, cap_bytes: int | None = None, busy_ms: int = 5_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cap_bytes = int(CAP_BYTES if cap_bytes is None else cap_bytes)
        self.conn = sqlite3.connect(str(self.path), timeout=busy_ms / 1000, isolation_level=None,
                                    check_same_thread=False)
        try:
            self.conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
            # Before the first table exists (a no-op on an older file): pruned pages can then go
            # back to the disk through incremental_vacuum instead of holding the high-water mark.
            self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute(f"PRAGMA journal_size_limit={WAL_LIMIT_BYTES}")
            self.conn.executescript(SCHEMA)
        except BaseException:
            with contextlib.suppress(Exception):
                self.conn.close()
            raise
        self.ops: list[tuple[str, tuple[Any, ...]]] = []
        self.first_op_ms: int | None = None
        #: At the cap: no text, metrics or payloads. Full: nothing but counters and state.
        self.capacity = False
        self.full = False
        self.failing = False
        #: Rows lost to a failed write, rows one-by-one refused, writes skipped while full.
        self.dropped = 0
        self.bad_rows = 0
        self.skipped = 0

    def add(self, sql: str, params: tuple[Any, ...], *, now_ms: int | None = None) -> None:
        if self.full and sql not in _ALWAYS_WRITTEN:
            self.skipped += 1  # the Recorder turns this into the `dropped:store_full` counter
            return
        if not self.ops:
            self.first_op_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.ops.append((sql, params))
        if len(self.ops) >= BATCH_ROWS:
            self.flush()

    def due(self, now_ms: int) -> bool:
        return bool(self.ops) and (len(self.ops) >= BATCH_ROWS or self.first_op_ms is None
                                   or now_ms - self.first_op_ms >= BATCH_MS)

    def flush(self) -> int:
        """Commit the queued rows; the number written. Never raises."""
        if not self.ops:
            return 0
        ops, first = self.ops, self.first_op_ms
        self.ops, self.first_op_ms = [], None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for sql, params in ops:
                self.conn.execute(sql, params)
            self.conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - a ValueError (lone surrogate) is not an sqlite3.Error
            self._rollback()
            if _environmental(exc):
                return self._lose(ops, exc)
            return self._commit_each(ops, first if first is not None else int(time.time() * 1000))
        self.failing = False
        return len(ops)

    def _commit_each(self, ops: list[tuple[str, tuple[Any, ...]]], at: int) -> int:
        """The batch again, one SAVEPOINT per row: only the rows SQLite refuses are lost."""
        written = bad = 0
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for sql, params in ops:
                self.conn.execute("SAVEPOINT j7_row")
                try:
                    self.conn.execute(sql, params)
                except Exception as exc:  # noqa: BLE001
                    if _environmental(exc):
                        raise
                    self.conn.execute("ROLLBACK TO j7_row")
                    bad += 1
                else:
                    written += 1
                self.conn.execute("RELEASE j7_row")
            if bad:
                self.conn.execute(SQL_COUNTER, ("dropped:row", bad, at))
            self.conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            self._rollback()
            return self._lose(ops, exc)
        self.failing = False
        self.bad_rows += bad
        if bad:
            log.warning("j7_feed: SQLite refused %d row(s) of a batch of %d; those were dropped, the rest kept",
                        bad, len(ops))
        return written

    def _lose(self, ops: list[tuple[str, tuple[Any, ...]]], exc: BaseException) -> int:
        self.failing = True
        self.dropped += len(ops)
        log.warning("j7_feed: store write failed (%s); %d queued rows dropped", type(exc).__name__, len(ops))
        return 0

    def _rollback(self) -> None:
        with contextlib.suppress(Exception):
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")

    def used_bytes(self) -> int:
        page_size = int(self.conn.execute("PRAGMA page_size").fetchone()[0])
        pages = int(self.conn.execute("PRAGMA page_count").fetchone()[0])
        free = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
        return (pages - free) * page_size

    def check_capacity(self) -> bool:
        """Set :attr:`capacity` and :attr:`full`; return :attr:`capacity`.

        Capacity at :attr:`cap_bytes` of used pages; full at :data:`FULL_FACTOR` times that
        or with the disk at :data:`DISK_FULL_FRACTION`. Each lifts once retention freed 10 %
        of its threshold (and, for full, the disk is under :data:`DISK_RESUME_FRACTION`).
        Full implies capacity.
        """
        try:
            used = self.used_bytes()
        except Exception:  # noqa: BLE001 - keep the last verdict
            return self.capacity
        disk = disk_fraction(self.path.parent)
        ceiling = self.cap_bytes * FULL_FACTOR
        if self.full:
            self.full = used >= ceiling * CAP_RESUME or (disk is not None and disk >= DISK_RESUME_FRACTION)
        else:
            self.full = used >= ceiling or (disk is not None and disk >= DISK_FULL_FRACTION)
        if self.capacity:
            self.capacity = self.full or used >= self.cap_bytes * CAP_RESUME
        else:
            self.capacity = self.full or used >= self.cap_bytes
        return self.capacity

    async def prune(self, now_ms: int, retention_ms: int = RETENTION_MS) -> int:
        """Delete rows older than ``retention_ms`` (14 days; 7 at the cap), at most
        :data:`PRUNE_BATCH` per transaction, then give the freed pages back to the disk."""
        cutoff = int(now_ms) - int(retention_ms)
        removed = 0
        for table, column in PRUNED:
            while True:
                try:
                    cur = self.conn.execute(
                        f"DELETE FROM {table} WHERE id IN "
                        f"(SELECT id FROM {table} WHERE {column} < ? LIMIT ?)",
                        (cutoff, PRUNE_BATCH),
                    )
                except sqlite3.Error as exc:
                    log.warning("j7_feed: retention on %s failed (%s)", table, type(exc).__name__)
                    break
                removed += max(cur.rowcount, 0)
                await asyncio.sleep(0)
                if cur.rowcount < PRUNE_BATCH:
                    break
        if removed:
            await self.vacuum()
        return removed

    async def vacuum(self) -> int:
        """Return free pages to the disk, :data:`VACUUM_STEP_PAGES` per short transaction;
        the pages freed. A file made before auto_vacuum was set frees nothing (no error)."""
        freed = 0
        for _ in range(VACUUM_MAX_STEPS):
            try:
                before = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
                if before <= 0:
                    break
                # fetchall() drives the pragma to completion; a bare execute frees one page
                self.conn.execute(f"PRAGMA incremental_vacuum({min(before, VACUUM_STEP_PAGES)})").fetchall()
                after = int(self.conn.execute("PRAGMA freelist_count").fetchone()[0])
            except sqlite3.Error as exc:
                log.warning("j7_feed: returning free pages failed (%s)", type(exc).__name__)
                break
            if after >= before:
                break
            freed += before - after
            await asyncio.sleep(0)
        return freed

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.flush()
        self._rollback()
        with contextlib.suppress(Exception):
            self.conn.close()


# ------------------------------------------------------------------------------- recorder


@dataclass(frozen=True)
class Post:
    post_id: str
    row: tuple[Any, ...]
    mentions: tuple[tuple[str, str, str | None], ...]


def build_post(payload: Mapping[str, Any], *, platform: str, now_ms: int, initial: int,
               deleted_ms: int | None, keep_text: bool, scrub: Callable[[str], str]) -> Post | None:
    """One ``posts`` row (in :data:`_ROW_COLUMNS` order) and its addresses, or ``None``.

    Reads a ``Tweet``, a ``SocialPost``, a ``WebhookCard`` or the ``RawTweet`` J7 attaches to
    a deletion (``author.profile.name``, ``body.text``, ``created_at``). Empty values become
    ``None`` so the merge never blanks a stored value with them.
    """
    pid = post_id_of(payload.get("id"))
    if pid is None:
        return None
    author = payload.get("author")
    author = author if isinstance(author, Mapping) else {}
    name = author.get("name")
    profile = author.get("profile")
    if name is None and isinstance(profile, Mapping):
        name = profile.get("name")
    text = payload.get("text")
    body = payload.get("body")
    if text is None and isinstance(body, Mapping):
        text = body.get("text")
    if keep_text and isinstance(text, str):
        # Cut BEFORE the work: cleaning and the scrub cost per character, and this runs on the
        # ingest loop every other listener shares. A secret that starts before TEXT_CHARS
        # still fits whole inside the window.
        text = text[: TEXT_CHARS + SCRUB_WINDOW]
        if platform != "x":
            text = html.unescape(text)  # J7: social post text is HTML-entity encoded; &#x202E; too
        text = scrub(strip_invisible(text)).strip()[:TEXT_CHARS] or None
    else:
        text = None
    display = _text(name, 100)
    author_id = _text(author.get("id"), 64)
    mentions: list[tuple[str, str, str | None]] = []
    for address, hint in contract_hints(payload):
        resolved = resolve_address(address, hint)
        if resolved is not None and all(resolved[:2] != m[:2] for m in mentions):
            mentions.append(resolved)
    first = mentions[0] if mentions else None
    row = (
        platform, pid, normalize_handle(author.get("handle")),
        author_id if author_id and _AUTHOR_ID.fullmatch(author_id) else None,
        scrub(display) if display else None, kind_of(payload, platform), text,
        created_ms_of(payload), now_ms, now_ms, deleted_ms, int(initial),
        ref_of(payload.get("quotedTweet")), ref_of(payload.get("replyTo")), media_count_of(payload),
        metrics_of(payload) if keep_text else None, coin_pairs_of(payload),
        first[1] if first else None, first[0] if first else None,
    )
    return Post(pid, row, tuple(mentions))


class Recorder:
    """J7 events -> store rows. Holds only bounded caches; the store is the state.

    :meth:`handle` returns ``(events counted, live post?)``: a live post is a ``tweet``,
    ``tweet_update``, ``tweet_deleted`` or social post that was recorded. The backlog, token
    metadata, follows and status are not, because a socket that only replays its backlog (or
    only answers pings) is not a live feed. Every copy of a post goes through
    :data:`SQL_MERGE`.
    """

    def __init__(self, store: Store, *, clock: Callable[[], int], secrets: Secrets) -> None:
        self.store = store
        self.clock = clock
        self.secrets = secrets
        #: post id -> (platform, initial) of recently recorded posts, for token_meta links.
        self.recent: OrderedDict[str, tuple[str, int]] = OrderedDict()
        #: post id -> token_meta addresses that arrived before their post.
        self.links: OrderedDict[str, list[tuple[str, str, str | None, int]]] = OrderedDict()
        self.meta_seen: OrderedDict[tuple[str, str], tuple[tuple[Any, ...], int]] = OrderedDict()
        self.counts: dict[str, int] = {}
        #: J7's upstream X feed as its ``status`` event last said (None: not told yet).
        self.upstream: bool | None = None

    # -- bookkeeping
    def count(self, name: str, n: int = 1) -> None:
        key = _COUNTER.sub("_", str(name))[:48] or "unnamed"
        if key not in self.counts and len(self.counts) >= COUNTER_NAMES:
            key = "other"
        self.counts[key] = self.counts.get(key, 0) + n

    def drain_counters(self) -> None:
        if self.store.skipped:
            self.count("dropped:store_full", self.store.skipped)
            self.store.skipped = 0
        if not self.counts:
            return
        now = self.clock()
        for name, n in self.counts.items():
            self.store.add(SQL_COUNTER, (name, n, now), now_ms=now)
        self.counts.clear()

    def expire_links(self, now: int) -> None:
        while self.links:
            items = next(iter(self.links.values()))
            if items and now - items[-1][3] < PENDING_LINK_MS:
                break
            self.links.popitem(last=False)

    def _add(self, sql: str, params: tuple[Any, ...], now: int) -> None:
        self.store.add(sql, params, now_ms=now)

    # -- dispatch
    def handle(self, event: str, payload: Any) -> tuple[int, bool]:
        now = self.clock()
        if event == "initialTweets":
            return self._initial(payload, now), False
        if event in ("tweet", "tweet_update"):
            ok = self._post(payload, now, initial=0)
            return int(ok), ok
        if event == "tweet_deleted":
            ok = self._deleted(payload, now)
            return int(ok), ok
        if event == "external_message":
            return self._external(payload, now)
        if event == "token_meta":
            return int(self._token_meta(payload, now)), False
        if event == "status":
            return int(self._status(payload, now)), False
        if event in SIDE_EVENTS:
            return int(self._side(event, payload, now)), False
        if event in COUNTED_EVENTS:
            self.count(f"ignored:{event}")
            return 1, False
        self.count(f"unknown:{event}")
        return 0, False

    # -- posts
    def _post(self, payload: Any, now: int, *, initial: int, deleted_ms: int | None = None) -> bool:
        if not isinstance(payload, Mapping):
            self.count("malformed:post")
            return False
        platform = platform_of(payload)
        post = build_post(payload, platform=platform, now_ms=now, initial=initial, deleted_ms=deleted_ms,
                          keep_text=not self.store.capacity, scrub=self.secrets.scrub)
        if post is None:
            self.count("malformed:post")
            return False
        self._add(SQL_MERGE, post.row, now)
        pid = post.post_id
        if pid in self.recent:
            self.recent.move_to_end(pid)
        else:
            self.recent[pid] = (platform, int(initial))
            while len(self.recent) > RECENT_IDS:
                self.recent.popitem(last=False)
        flag = self.recent[pid][1]
        for chain, address, hint in post.mentions:
            self._mention(platform, pid, chain, address, hint, "post", now, flag)
        for chain, address, hint, at in self.links.pop(pid, ()):
            if now - at <= PENDING_LINK_MS:
                self._mention(platform, pid, chain, address, hint, "token_meta", now, flag)
        return True

    def _mention(self, platform: str, pid: str, chain: str, address: str, hint: str | None,
                 source: str, now: int, initial: int) -> None:
        self._add(SQL_MENTION, (platform, pid, chain, address, hint, source, now, initial), now)
        self._add(SQL_SET_CA, (address, chain, platform, pid), now)

    def _initial(self, payload: Any, now: int) -> int:
        if not isinstance(payload, list):
            self.count("malformed:initialTweets")
            return 0
        recorded = 0
        for item in payload[:INITIAL_MAX_ITEMS]:
            if isinstance(item, Mapping) and str(item.get("type") or "").upper() in PATCH_TYPES:
                continue
            recorded += self._post(item, now, initial=1)
        if len(payload) > INITIAL_MAX_ITEMS:
            self.count("truncated:initialTweets", len(payload) - INITIAL_MAX_ITEMS)
        return recorded

    def _deleted(self, payload: Any, now: int) -> bool:
        if not isinstance(payload, Mapping):
            self.count("malformed:tweet_deleted")
            return False
        for key in ("deletedTweet", "tweet"):  # a full Tweet first, then J7's RawTweet
            copy = payload.get(key)
            if isinstance(copy, Mapping) and post_id_of(copy.get("id")):
                return self._post(copy, now, initial=0, deleted_ms=now)
        pid = post_id_of(payload.get("id"))
        if pid is None:
            self.count("malformed:tweet_deleted")
            return False
        self._add(SQL_MARK_DELETED, (now, now, "x", pid), now)  # known posts only: no row invented
        return True

    def _external(self, payload: Any, now: int) -> tuple[int, bool]:
        if not isinstance(payload, Mapping):
            self.count("malformed:external_message")
            return 0, False
        label = str(payload.get("type") or "").upper()
        if label in PATCH_TYPES:
            if label != "YOUTUBE_DELETION":
                self.count(f"patch:{label.lower()}")
                return 1, False
            pid = post_id_of(payload.get("id"))
            if pid is None:
                self.count("malformed:external_message")
                return 0, False
            self._add(SQL_MARK_DELETED, (now, now, "youtube", pid), now)
            return 1, True
        deleted = now if payload.get("youtubeDeleted") is True else None
        ok = self._post(payload, now, initial=0, deleted_ms=deleted)
        return int(ok), ok

    # -- token metadata
    def _token_meta(self, payload: Any, now: int) -> bool:
        if not isinstance(payload, Mapping):
            self.count("malformed:token_meta")
            return False
        meta = payload.get("tokenMeta")
        meta = meta if isinstance(meta, Mapping) else {}
        resolved = resolve_address(payload.get("mint") or meta.get("mint"), meta.get("chain"))
        if resolved is None:
            self.count("malformed:token_meta")
            return False
        chain, address, hint = resolved
        parent = post_id_of(payload.get("id"))
        scrub = self.secrets.scrub
        symbol, name, market = (_text(meta.get(k), n) for k, n in (("symbol", 32), ("name", 100), ("market", 32)))
        values = (
            parent, scrub(symbol) if symbol else None, scrub(name) if name else None,
            scrub(market) if market else None, _usd(meta.get("marketCapUsd")),
            _usd(meta.get("liquidityUsd")), _flag(meta.get("isHoneypot")),
        )
        key = (chain, address)
        last = self.meta_seen.get(key)
        if last is not None and last[0] == values and now - last[1] < TOKEN_META_REPEAT_MS:
            self.count("repeat:token_meta")
            return True
        self.meta_seen[key] = (values, now)
        self.meta_seen.move_to_end(key)
        while len(self.meta_seen) > META_SEEN:
            self.meta_seen.popitem(last=False)
        self._add(SQL_TOKEN_META, (chain, address, *values, now), now)
        if parent is not None:
            known = self.recent.get(parent)
            if known is not None:
                self._mention(known[0], parent, chain, address, hint, "token_meta", now, known[1])
            else:
                pending = self.links.setdefault(parent, [])
                self.links.move_to_end(parent)
                if len(pending) < 8:
                    pending.append((chain, address, hint, now))
                while len(self.links) > PENDING_LINKS:
                    self.links.popitem(last=False)
        return True

    # -- the rest
    def _status(self, payload: Any, now: int) -> bool:
        connected = payload.get("connected") if isinstance(payload, Mapping) else None
        if not isinstance(connected, bool):
            self.count("malformed:status")
            return False
        if connected is not self.upstream:  # stored on change only
            self.upstream = connected
            self._add(SQL_EVENT, ("status", None, now, json.dumps({"connected": connected})), now)
        return True

    def _side(self, event: str, payload: Any, now: int) -> bool:
        if not isinstance(payload, Mapping):
            self.count(f"malformed:{event}")
            return False
        user = payload.get("user")
        handle = normalize_handle((user.get("handle") if isinstance(user, Mapping) else None)
                                  or payload.get("handle"))
        blob = None if self.store.capacity else self.secrets.scrub(bounded_json(payload))
        self._add(SQL_EVENT, (event, handle, now, blob), now)
        return True


# --------------------------------------------------------------------------------- health


class Health:
    """The feed's ``ingest_status`` row, written through :func:`kaiba.ingest.runner.set_status`.

    At most one write per :data:`STATUS_EVERY_MS`, and only when the state, the error, the
    event count or the last post time changed, or a new start is pending.
    ``last_event_ms`` moves only on a live post. ``restarts`` counts sockets reopened since
    the listener started.

    ``started_ms`` is written on the first write of each :func:`run`, again when a refusal
    ends (``down`` -> ``running``), and when a different token is tried
    (:meth:`new_attempt`): the pager's silence window then starts when the feed could first
    produce, not when kaiba-ingest started hours earlier (a turn-on through the runner's 6 h
    idle recheck paged "silent 6.0h" within seconds), and a down page is one per token and
    code. It is NOT reset by a reconnect or a stall, or the 15-minute stall reconnect would
    hold the 30-minute silence page off forever.
    """

    def __init__(self, conn: Any, clock: Callable[[], int]) -> None:
        self.conn = conn
        self.clock = clock
        self.state: str | None = None
        self.base_error: str | None = None
        self.upstream_down = False
        self.capacity = False
        self.full = False
        self.store_error = False
        #: The session token's ``exp`` (ms) while it is known; ``token_expiring`` an hour before.
        self.token_exp_ms: int | None = None
        self.events = 0
        self.post_ms: int | None = None
        self.restarts = 0
        self.last_write_ms: int | None = None
        self.written: tuple[str | None, str | None] | None = None
        self.writes = 0
        #: The ``started_ms`` the next write carries: this run's start, then a refusal's end.
        self.pending_start_ms: int | None = clock()

    @property
    def error(self) -> str | None:
        codes = [self.base_error]
        if self.full:
            codes.append("store_full")
        elif self.capacity:
            codes.append("store_capacity")
        if self.store_error:
            codes.append("store_error")
        if (self.state in ("running", "reconnecting") and self.token_exp_ms is not None
                and self.clock() >= self.token_exp_ms - TOKEN_WARN_MS):
            codes.append(f"token_expiring:{self.token_exp_ms // 1000}")
        if self.upstream_down and self.state == "running":
            codes.append("upstream:disconnected")
        return ",".join(c for c in codes if c) or None

    def set(self, state: str, error: str | None = None) -> None:
        if state == "running" and self.state == "down":
            self.pending_start_ms = self.clock()  # a refusal ended: a full silence window from now
        self.state, self.base_error = state, _stable(error)

    def new_attempt(self) -> None:
        """The listener is about to try a DIFFERENT token: a new episode. ``started_ms`` moves
        on the next write, even when state and error stay the same. The pager keys a j7_feed
        down page on ``started_ms`` and the code (kaiba/ops/trade_notify.py), so a refreshed
        token that J7 refuses with the same code as the old one pages again; until 2026-10-28
        the row was not even rewritten and Telegram stayed silent after the owner did what the
        page said (integration review r3). It also gives the new token a full silence window."""
        self.pending_start_ms = self.clock()

    def note(self, events: int, live_post: bool) -> None:
        self.events += max(int(events), 0)
        if live_post:
            self.post_ms = self.clock()

    def flush(self, *, force: bool = False) -> bool:
        if self.state is None:
            return False
        now = self.clock()
        key = (self.state, self.error)
        if (key == self.written and not self.events and self.post_ms is None
                and self.pending_start_ms is None):
            return False
        if not force and self.last_write_ms is not None and now - self.last_write_ms < STATUS_EVERY_MS:
            return False
        from kaiba.ingest import runner  # lazy: the runner imports the listeners

        runner.set_status(FEED, self.state, self.conn, events_seen=self.events, last_event_ms=self.post_ms,
                          last_error=key[1], restarts=self.restarts, started_ms=self.pending_start_ms)
        self.written, self.events, self.post_ms, self.pending_start_ms = key, 0, None, None
        self.last_write_ms = now
        self.writes += 1
        return True


# ----------------------------------------------------------------------------- connection


def _rand() -> float:
    """Indirection so the reconnect schedule is deterministic under test."""
    return random.random()


def backoff_delay(failures: int) -> float:
    """5 s doubling to 300 s, plus up to 25 % jitter, capped after the jitter."""
    raw = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** min(max(int(failures), 0), 16))
    return round(min(BACKOFF_CAP_S, raw * (1.0 + BACKOFF_JITTER * _rand())), 3)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, waking early on stop. Tests patch this (and move their clock)."""
    if seconds <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


def region_of(value: str | None = None) -> str:
    """``J7_FEED_REGION`` (``nyc`` or ``dfw``); anything else is the default, ``nyc``."""
    raw = (os.environ.get(REGION_ENV, "") if value is None else value).strip().lower()
    if raw and raw not in HOSTS:
        log.warning("j7_feed: %s=%r is not one of %s; using %s", REGION_ENV, raw[:16], sorted(HOSTS),
                    DEFAULT_REGION)
    return raw if raw in HOSTS else DEFAULT_REGION


def _other_region(region: str) -> str:
    others = [name for name in HOSTS if name != region]
    return others[0] if others else region


def auth_code(message: Any) -> str | None:
    """The stable code for one of J7's refusal messages, or ``None`` for anything else."""
    if isinstance(message, Mapping):
        message = message.get("error") or message.get("message")
    if not isinstance(message, str):
        return None
    low = message.strip().lower()
    for needle, code in AUTH_CODES.items():
        if needle in low:
            return code
    return None


def refusal_code(error: Any) -> str | None:
    """What ``connect_error`` received, as a stable code: one of J7's documented refusals;
    :data:`REFUSED_CODE` for any other namespace refusal (a mapping with a ``message``); and
    ``None`` for a transport failure (a plain string) or nothing at all."""
    code = auth_code(error)
    if code is not None or not isinstance(error, Mapping):
        return code
    message = error.get("message")
    return REFUSED_CODE if isinstance(message, str) and message.strip() else None


def _available(name: str) -> bool:
    """Whether ``import name`` can succeed, found without importing it."""
    if name in sys.modules:
        return sys.modules[name] is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _socketio_factory() -> Callable[[], Any] | None:
    """A factory for python-socketio's asyncio client, or ``None`` when it cannot work here.

    That client needs aiohttp as well as python-socketio. The ``social`` extra installs both;
    J7's own install line (``python-socketio[client]``) and a bare ``python-socketio`` install
    only the first. Without aiohttp python-engineio logs "aiohttp package not installed" and
    RETURNS instead of raising, python-socketio then waits out ``wait_timeout`` and raises a
    ConnectionError, and every connect would fail as ``connect_failed`` and be retried for ever
    with no instruction anywhere (protocol review, 2026-10-28). Both cases are
    ``missing_socketio_dependency``, whose page says to install the extra.
    """
    if not _available("aiohttp"):
        return None
    try:
        import socketio  # optional: the `social` extra
    except ImportError:
        return None
    engine = sys.modules.get("engineio.async_client")
    if engine is not None and getattr(engine, "aiohttp", True) is None:
        return None  # aiohttp is installed but does not import: engine.io fell back to None

    def make() -> Any:
        try:
            return socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False,
                                        handle_sigint=False)
        except TypeError:  # a release without handle_sigint
            return socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)

    return make


def _consume(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


def _is_connected(client: Any) -> bool:
    """python-socketio's ``connected`` flag AND engine.io's ``state``: False once either says
    the socket dropped. ``connected`` alone misses one window: a transport that dies after the
    namespace CONNECT but before ``connect()`` returns fires no ``disconnect`` (the client is
    not ``connected`` yet when engine.io reports the drop), and ``connect()`` then sets
    ``connected`` True anyway, while engine.io's state already reads ``disconnected``. A client
    without these attributes is judged by its ``disconnect`` event alone."""
    if getattr(client, "connected", True) is False:
        return False
    engine = getattr(client, "eio", None)
    return engine is None or getattr(engine, "state", "connected") == "connected"


async def _close(client: Any) -> None:
    """Disconnect, bounded by :data:`CLOSE_TIMEOUT_S`; never raises out of the listener.

    python-socketio >= 5.13 (what ``>=5.11,<6`` installs) disconnects with engine.io's
    ``abort=False``, which ends by awaiting engine.io's read-loop task. A ``CancelledError``
    from there is the library's, not ours, so it is swallowed like any other error; a
    cancellation of THIS task (``cancelling()``) still propagates. The listener itself never
    cancels that read loop: it holds no task awaiting ``client.wait()`` (2026-10-28: a
    cancelled waiter cancelled the read loop, and the CancelledError escaped ``run()`` on
    every session exit while the socket was open, ending the feed until kaiba-ingest restarted).
    """
    try:
        await asyncio.wait_for(client.disconnect(), timeout=CLOSE_TIMEOUT_S)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
    except Exception:  # noqa: BLE001 - a socket we are leaving anyway
        pass


@dataclass
class _Session:
    started_ms: int
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    handshaked: bool = False
    connect_error: Any = None
    auth_code: str | None = None


@dataclass(frozen=True)
class _Outcome:
    #: "auth" | "connect_failed" | "ended" | "token" | "stop"
    kind: str
    code: str | None = None
    healthy: bool = False


class Feed:
    """One J7 socket at a time, reconnected with backoff, recorded through :class:`Recorder`."""

    def __init__(self, *, store: Store, token_reader: Callable[[], str], client_factory: Callable[[], Any],
                 clock: Callable[[], int], conn: Any, region: str, secrets: Secrets) -> None:
        self.store = store
        self.recorder = Recorder(store, clock=clock, secrets=secrets)
        self.token_reader = token_reader
        self.client_factory = client_factory
        self.clock = clock
        self.region = region
        self.secrets = secrets
        self.health = Health(conn, clock)
        self.last_post_ms: int | None = None
        self.session: _Session | None = None
        #: The token whose expiry was last noted (its fingerprint) and that expiry.
        self.token_fp: str | None = None
        self.token_exp_ms: int | None = None
        #: The fingerprint of the token the loop last tried (connected with, or found expired).
        self.tried_fp: str | None = None
        #: Since when every re-read found the token file unusable (a FILE_FAULTS code).
        self.file_fault_since_ms: int | None = None
        start = clock()
        self.next_prune_ms = start
        self.next_capacity_ms = start
        self.next_token_ms = start + TOKEN_RECHECK_MS

    # -- token
    def _token(self) -> str:
        try:
            value = self.token_reader()
        except FeedUnavailable:
            raise
        except Exception:  # noqa: BLE001 - a broken reader is a bad token, never a crash
            raise FeedUnavailable("invalid_j7_session") from None
        if not usable_token(value):
            raise FeedUnavailable("invalid_j7_session")
        self.secrets.add(value)
        return value

    def _token_verdict(self, fingerprint: str) -> str | None:
        """A re-read of the token file, for the token in use (``fingerprint``).

        ``None`` while it is unchanged; ``changed`` for another usable token; ``gone:<code>``
        when the file is gone (the owner's off switch) or :data:`TOKEN_ENV` is set: both end
        the run at once. A FILE_FAULTS code (empty, not one line, wrong owner or mode,
        unreadable) is ``None`` until every re-read for :data:`TOKEN_FILE_GRACE_MS` found it,
        and ``bad:<code>`` after that: the owner may be rewriting the file (`cat >` truncates
        it first) or about to chmod it, and the token in use stays in use meanwhile.
        """
        now = self.clock()
        try:
            value = self._token()
        except FeedUnavailable as exc:
            code = _code(exc)
            if code not in FILE_FAULTS:
                self.file_fault_since_ms = None
                return f"gone:{code}"
            if self.file_fault_since_ms is None:
                self.file_fault_since_ms = now
                log.info("j7_feed: the session token file reads %s; the session in use is kept for up to %d s "
                         "while the file is rewritten", code, TOKEN_FILE_GRACE_MS // 1000)
            if now - self.file_fault_since_ms >= TOKEN_FILE_GRACE_MS:
                return f"bad:{code}"
            return None
        self.file_fault_since_ms = None
        return None if _fingerprint(value) == fingerprint else "changed"

    async def _wait_for_file(self, stop: asyncio.Event, code: str) -> str | None:
        """The token file is there and unusable (a FILE_FAULTS ``code``) with no session to
        keep: no socket, ``down`` with the code (it pages after the pager's grace), and the
        file re-read every :data:`TOKEN_RECHECK_MS`. ``gone:<code>`` once the file is gone or
        the variable is set; ``None`` once it holds a usable token again, or on stop.

        It does not return idle: the runner would park the feed for 6 h, and the owner's fix
        would wait that long instead of 30 s."""
        self._note_token(None, None)
        log.warning("j7_feed: the session token file reads %s; not connecting until it holds one usable "
                    "token (re-read every %d s)", code, TOKEN_RECHECK_MS // 1000)
        while not stop.is_set():
            self.health.set("down", code)
            self.health.flush()
            await self._sleep(stop, TOKEN_RECHECK_MS / 1000)
            if stop.is_set():
                break
            try:
                self._token()
            except FeedUnavailable as exc:
                code = _code(exc)
                if code not in FILE_FAULTS:
                    return f"gone:{code}"
                continue
            return None
        return None

    def _note_token(self, token: str | None, fingerprint: str | None) -> int | None:
        """The token's ``exp`` (ms), noted once per token: one INFO line with the lifetime,
        the health row's ``token_expiring`` clock and ``feed_state`` for ``kaiba_tweets``.
        ``None`` (also stored) for an opaque token, one without ``exp``, or no token."""
        if fingerprint == self.token_fp:
            return self.token_exp_ms
        exp = token_expiry_ms(token) if token else None
        self.token_fp, self.token_exp_ms = fingerprint, exp
        self.health.token_exp_ms = exp
        now = self.clock()
        self.store.add(SQL_STATE, ("token_expires_ms", exp, now), now_ms=now)
        if token is None:
            pass
        elif exp is None:
            log.info("j7_feed: the session token has no readable expiry (not a JWT, or no exp claim)")
        elif exp > now:
            log.info("j7_feed: the session token expires in %.1f h (%s UTC)", (exp - now) / 3_600_000, _utc(exp))
        # (an expired one is logged by run(), which then does not connect)
        return exp

    # -- the loop
    async def run(self, stop: asyncio.Event) -> dict[str, Any]:
        region = self.region
        failures = 0
        refusals = 0
        refused: str | None = None
        opened = 0
        try:
            while not stop.is_set():
                try:
                    token = self._token()
                except FeedUnavailable as exc:
                    code = _code(exc)
                    if code in FILE_FAULTS:
                        verdict = await self._wait_for_file(stop, code)
                        if verdict is None:
                            continue  # usable again (or stop): the loop re-reads it
                        code = verdict.split(":", 1)[1]
                    self._note_token(None, None)
                    return {"idle": code}
                self.file_fault_since_ms = None
                fingerprint = _fingerprint(token)
                if self.tried_fp is not None and fingerprint != self.tried_fp:
                    self.health.new_attempt()  # a different token: its own episode (and page)
                self.tried_fp = fingerprint
                exp = self._note_token(token, fingerprint)
                if exp is not None and self.clock() >= exp:
                    # J7 would answer "Invalid token" ("expired ... don't retry with the same
                    # token"): do not connect at all; re-read the file every 30 s for a new one.
                    del token
                    self.health.set("down", "auth_error:token_expired")
                    self.health.flush()
                    log.warning("j7_feed: the J7 session token expired at %s UTC; not connecting until "
                                "the file holds a new one", _utc(exp))
                    verdict = await self._sleep(stop, AUTH_BACKOFF_CAP_S, fingerprint=fingerprint)
                    if verdict and verdict.startswith("gone:"):
                        self._note_token(None, None)
                        return {"idle": verdict.split(":", 1)[1]}
                    continue
                if opened:
                    self.health.restarts += 1
                opened += 1
                outcome = await self._session(stop, token, region)
                del token
                if outcome.kind == "stop" or stop.is_set():
                    break
                if outcome.kind == "token":
                    if (outcome.code or "").startswith("gone:"):
                        self._note_token(None, None)
                        return {"idle": str(outcome.code).split(":", 1)[1]}
                    failures = refusals = 0
                    continue
                if outcome.kind == "auth":
                    if outcome.code == "auth_error:too_many_connections":
                        # J7: "close one" of your sockets -- the token is fine, so no doubling.
                        wait_s = AUTH_BACKOFF_S
                    else:
                        refusals = refusals + 1 if refused == fingerprint else 1
                        refused = fingerprint
                        wait_s = min(AUTH_BACKOFF_CAP_S, AUTH_BACKOFF_S * 2 ** (refusals - 1))
                    self.health.set("down", outcome.code)
                    self.health.flush()
                    log.warning("j7_feed: J7 refused the session token (%s); the same token is tried "
                                "again in %.0f s, a new one at once", outcome.code, wait_s)
                    verdict = await self._sleep(stop, wait_s, fingerprint=fingerprint)
                    if verdict and verdict.startswith("gone:"):
                        self._note_token(None, None)
                        return {"idle": verdict.split(":", 1)[1]}
                    continue
                if outcome.healthy:
                    failures = refusals = 0
                    refused = None
                if (outcome.kind == "connect_failed" or (outcome.code or "").startswith("stalled:")
                        or (outcome.kind == "ended" and not outcome.healthy)):
                    # The other host carries the same feed (J7). A drop after a healthy
                    # session (a node restart) retries this one first.
                    region = _other_region(region)
                delay = backoff_delay(failures)
                failures += 1
                self.health.set("reconnecting", outcome.code)
                self.health.flush()
                log.info("j7_feed: %s; reconnecting to %s in %.1f s", outcome.code, region, delay)
                await self._sleep(stop, delay)
            return {"stopped": FEED}
        finally:
            self._flush_store()
            self.health.flush(force=True)

    async def _session(self, stop: asyncio.Event, token: str, region: str) -> _Outcome:
        s = _Session(started_ms=self.clock())
        self.session = s
        # J7's `status` (its upstream X feed) was said to the LAST socket, and nothing says it
        # is re-sent when one connects. Carried over, a `connected: false` switched off this
        # socket's stall reconnect for its whole life and kept `upstream:disconnected` on its
        # row, so the silence page said "nothing to fix on Kaiba's side" (protocol review,
        # 2026-10-28). Only what THIS socket is told counts.
        self.recorder.upstream = None
        self.health.upstream_down = False
        try:
            client = self.client_factory()
            self._bind(client, token, s)
        except Exception as exc:  # noqa: BLE001
            log.warning("j7_feed: could not build the Socket.IO client (%s)", type(exc).__name__)
            self.session = None
            return _Outcome("connect_failed", f"connect_failed:{region}")
        try:
            refused = await self._connect(client, stop, token, region, s)
            if refused is not None:
                return refused
            self.health.set("running", None)
            self.health.flush()
            log.info("j7_feed: connected to %s", region)
            fingerprint = _fingerprint(token)
            self.next_token_ms = self.clock() + TOKEN_RECHECK_MS
            # No task awaits client.wait(): it awaits engine.io's read loop itself, and
            # cancelling it cancels that loop (see _close). The end of a session is the
            # `disconnect` event, which python-socketio fires once connected, or `connected`.
            while True:
                if stop.is_set():
                    return _Outcome("stop")
                if s.auth_code:
                    return _Outcome("auth", s.auth_code)
                if s.ended.is_set() or not _is_connected(client):
                    return _Outcome("ended", f"disconnected:{region}", self._healthy(s))
                now = self.clock()
                if (self.recorder.upstream is not False
                        and now - max(s.started_ms, self.last_post_ms or 0) >= STALL_MS):
                    log.warning("j7_feed: no live post for %d min on %s; reconnecting",
                                STALL_MS // 60_000, region)
                    return _Outcome("ended", f"stalled:{region}", self._healthy(s))
                if now >= self.next_token_ms:
                    self.next_token_ms = now + TOKEN_RECHECK_MS
                    verdict = self._token_verdict(fingerprint)
                    if verdict:
                        return _Outcome("token", verdict)
                await self._housekeeping(now)
                await self._tick(stop, s)
        finally:
            await _close(client)
            self._flush_store()
            self.session = None

    async def _connect(self, client: Any, stop: asyncio.Event, token: str, region: str,
                       s: _Session) -> _Outcome | None:
        """``client.connect()`` raced against ``stop`` and :data:`CONNECT_DEADLINE_S`.

        ``None`` once connected with the handshake sent; otherwise the outcome. On stop or the
        deadline the attempt is cancelled (the caller then closes the client): ``stop``, or
        ``connect_failed:<region>``, which fails over to the other region.
        """
        attempt = asyncio.ensure_future(client.connect(HOSTS[region], transports=["websocket"],
                                                       auth={"token": token}, wait_timeout=CONNECT_TIMEOUT_S))
        attempt.add_done_callback(_consume)
        stopped = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait({attempt, stopped}, timeout=CONNECT_DEADLINE_S,
                                         return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            attempt.cancel()
            raise
        finally:
            stopped.cancel()
        if attempt not in done:
            attempt.cancel()
            await asyncio.wait({attempt}, timeout=CLOSE_TIMEOUT_S)
            if stop.is_set():
                return _Outcome("stop")
            log.warning("j7_feed: connecting to %s took over %d s; giving up on it", region, CONNECT_DEADLINE_S)
            return _Outcome("connect_failed", f"connect_failed:{region}")
        error = asyncio.CancelledError() if attempt.cancelled() else attempt.exception()
        if error is not None:  # classified here, never re-raised
            code = s.auth_code or refusal_code(s.connect_error)
            if code == REFUSED_CODE:
                log.warning("j7_feed: J7 refused the connection with a message it does not document (%s); "
                            "treated as a refusal of this token", self._shown(s.connect_error))
            if code:
                return _Outcome("auth", code)
            log.warning("j7_feed: connecting to %s failed (%s)", region, type(error).__name__)
            return _Outcome("connect_failed", f"connect_failed:{region}")
        if stop.is_set():
            return _Outcome("stop")
        if s.auth_code:
            return _Outcome("auth", s.auth_code)
        if not s.handshaked:
            return _Outcome("connect_failed", f"connect_failed:{region}")
        return None

    def _healthy(self, s: _Session) -> bool:
        """Whether the session lasted :data:`HEALTHY_SESSION_MS`. A post does not make it so:
        a node that sends one and drops the socket is not a working node."""
        return self.clock() - s.started_ms >= HEALTHY_SESSION_MS

    def _shown(self, error: Any) -> str:
        """J7's own refusal text, fit for the log: the token scrubbed out FIRST (the cut below
        would break an exact match), then at most 80 plain characters. Never post text."""
        message = error.get("message") if isinstance(error, Mapping) else None
        if not isinstance(message, str):
            return "?"
        text = self.secrets.scrub(strip_invisible(message[:200]))
        return re.sub(r"[^A-Za-z0-9 .,:;'()<>_\-]+", "?", text).strip()[:80] or "?"

    async def _tick(self, stop: asyncio.Event, s: _Session) -> None:
        if s.ended.is_set() or stop.is_set():
            return
        s.wake.clear()
        timeout = TICK_S
        if self.store.ops and self.store.first_op_ms is not None:  # wake when the batch is due (<= 1 s)
            timeout = min(timeout, max(0.05, (self.store.first_op_ms + BATCH_MS - self.clock()) / 1000))
        waits = [asyncio.ensure_future(s.wake.wait()), asyncio.ensure_future(stop.wait())]
        try:
            await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()

    async def _sleep(self, stop: asyncio.Event, seconds: float, *, fingerprint: str | None = None) -> str | None:
        """Wait in :data:`WAIT_STEP_S` steps, keeping house; with ``fingerprint`` re-read the
        token every :data:`TOKEN_RECHECK_MS` and return its verdict as soon as it is not None."""
        remaining = float(seconds)
        since_check = 0.0
        while remaining > 0 and not stop.is_set():
            step = min(WAIT_STEP_S, remaining)
            await _wait(stop, step)
            remaining -= step
            since_check += step
            await self._housekeeping(self.clock())
            if fingerprint is not None and since_check * 1000 >= TOKEN_RECHECK_MS:
                since_check = 0.0
                verdict = self._token_verdict(fingerprint)
                if verdict:
                    return verdict
        return None

    async def _housekeeping(self, now: int) -> None:
        """Flush, the cap, retention and the status row. Never raises: a fault here is
        counted and logged, and the socket stays up."""
        try:
            await self._keep_house(now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.recorder.count("error:housekeeping")
            log.warning("j7_feed: housekeeping failed (%s)", type(exc).__name__)

    async def _keep_house(self, now: int) -> None:
        self.recorder.expire_links(now)
        if self.store.due(now):
            self._flush_store()
        if now >= self.next_capacity_ms:
            self.next_capacity_ms = now + CAPACITY_CHECK_MS
            before = (self.store.capacity, self.store.full)
            self.store.check_capacity()
            self.health.capacity, self.health.full = self.store.capacity, self.store.full
            if (self.store.capacity, self.store.full) != before:
                log.warning("j7_feed: store %s (%d MiB cap, disk at %s); %s",
                            "FULL" if self.store.full else "at its cap" if self.store.capacity else "back under its cap",
                            self.store.cap_bytes // 2**20, _percent(disk_fraction(self.store.path.parent)),
                            "nothing but counters is stored" if self.store.full
                            else "text and payloads are no longer stored, retention is 7 days" if self.store.capacity
                            else "text and payloads are stored again")
                if self.store.capacity and not before[0]:
                    self.next_prune_ms = now  # at the cap: prune now, at the shorter retention
        if now >= self.next_prune_ms:
            self.next_prune_ms = now + PRUNE_EVERY_MS
            self._flush_store()
            retention = CAPACITY_RETENTION_MS if self.store.capacity else RETENTION_MS
            removed = await self.store.prune(now, retention)
            if removed:
                log.info("j7_feed: retention removed %d rows older than %d days", removed, retention // 86_400_000)
        self.health.flush()

    def _flush_store(self) -> None:
        """Counters, then the batch. Never raises (a failure is logged and shown as store_error)."""
        try:
            self.recorder.drain_counters()
            self.store.flush()
        except Exception as exc:  # noqa: BLE001
            self.store.failing = True
            log.warning("j7_feed: flushing the store failed (%s)", type(exc).__name__)
        self.health.store_error = self.store.failing

    # -- socket handlers
    def _bind(self, client: Any, token: str, s: _Session) -> None:
        async def on_connect(*_args: Any) -> None:
            try:
                await client.emit(SIO_USER_CONNECTED, token)  # J7: "nothing arrives until you do"
                s.handshaked = True
            except Exception as exc:  # noqa: BLE001
                log.warning("j7_feed: the user_connected handshake failed (%s)", type(exc).__name__)
                s.ended.set()
                s.wake.set()

        async def on_connect_error(*args: Any) -> None:
            s.connect_error = args[0] if args else None

        async def on_disconnect(*_args: Any) -> None:
            s.ended.set()
            s.wake.set()

        async def on_auth_error(*args: Any) -> None:
            s.auth_code = auth_code(args[0] if args else None) or "auth_error"
            s.ended.set()
            s.wake.set()

        async def on_unknown(event: Any, *_args: Any) -> None:
            self.recorder.count(f"unknown:{event}")

        client.on("connect", on_connect)
        client.on("connect_error", on_connect_error)
        client.on("disconnect", on_disconnect)
        client.on("auth_error", on_auth_error)
        for name in sorted(RECORDED_EVENTS | COUNTED_EVENTS):
            client.on(name, self._receiver(name))
        client.on("*", on_unknown)

    def _receiver(self, event: str) -> Callable[..., Any]:
        async def receive(*args: Any) -> None:
            self._receive(event, args[0] if args else None)

        return receive

    def _receive(self, event: str, payload: Any) -> None:
        try:
            counted, live = self.recorder.handle(event, payload)
        except Exception as exc:  # noqa: BLE001 - one bad frame never ends the session
            self.recorder.count(f"error:{event}")
            log.warning("j7_feed: a %s event could not be recorded (%s)", event, type(exc).__name__)
            return
        if event == "status":
            self.health.upstream_down = self.recorder.upstream is False
        if live:
            self.last_post_ms = self.clock()
        self.health.note(counted, live)


# ------------------------------------------------------------------------------- listener


async def run(
    stop: asyncio.Event,
    *,
    client_factory: Callable[[], Any] | None = None,
    token_reader: Callable[[], str] | None = None,
    store_path: Path | str | None = None,
    clock: Callable[[], int] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """The ``j7_feed`` listener (kaiba/ingest/runner.py). Never raises into the runner.

    Returns ``{"idle": "missing_j7_session"}`` / ``"invalid_j7_session"`` /
    ``"insecure_j7_session"`` / ``"unreadable_j7_session"`` before any network use while
    there is no usable session token (:func:`file_session_token`; once the feed RUNS, only a
    deleted file ends it, see :data:`FILE_FAULTS`), ``{"idle": "j7_session_in_env"}`` while
    ``J7_SESSION_JWT`` is set (:func:`file_session_token`), ``{"idle":
    "missing_socketio_dependency"}`` when python-socketio's asyncio client (the ``social``
    extra: python-socketio and aiohttp) cannot run, and ``{"stopped": "j7_feed"}`` on stop.
    ``conn`` is the ``ingest_status`` connection (the runner's by default).

    Every idle return first runs retention on a store file that already exists
    (:func:`prune_idle_store`; local, no network, and no file is created).
    """
    reader = token_reader or file_session_token
    tick = clock or now_ms
    try:
        token = reader()
    except FeedUnavailable as exc:
        code = _code(exc)
        if code == "j7_session_in_env":
            log.warning("j7_feed: %s is set in this process's environment; the feed reads its token only "
                        "from ~/.config/kaiba/j7.session and does not run while the variable is set. Move "
                        "the token to that file and remove the variable from kaiba-ingest's environment.",
                        TOKEN_ENV)
        return await _idle(code, store_path, tick)
    except Exception:  # noqa: BLE001 - a broken reader is a bad token, never a crash
        return await _idle("invalid_j7_session", store_path, tick)
    usable = usable_token(token)
    del token
    if not usable:
        return await _idle("invalid_j7_session", store_path, tick)
    factory = client_factory or _socketio_factory()
    if factory is None:
        return await _idle("missing_socketio_dependency", store_path, tick)
    secrets = Secrets()
    filt = _install_filter(secrets)
    store: Store | None = None
    try:
        store = Store(Path(store_path) if store_path is not None else default_store_path())
        feed = Feed(store=store, token_reader=reader, client_factory=factory, clock=tick,
                    conn=conn, region=region_of(), secrets=secrets)
        return await feed.run(stop)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - never raise into the runner
        log.error("j7_feed: stopped on an internal error (%s)", type(exc).__name__)
        return {"idle": f"internal_error:{type(exc).__name__}"[:64]}
    finally:
        if store is not None:
            store.close()
        _remove_filter(filt)


#: The idle prune gives way at once to another writer of the store (there should be none).
IDLE_BUSY_MS = 250


async def prune_idle_store(store_path: Path | str | None = None, *,
                           clock: Callable[[], int] | None = None) -> int:
    """Retention for a store no running feed looks after: the rows removed.

    Pruning ran only inside a live feed, so once the owner switched it off (the documented
    off switch: delete the token file) its last 14 days stayed on the disk for good, a file
    neither kaiba-disk-guard nor the hourly retention job knows (integration review r3). The
    runner tries an idle listener again every 6 h, so this runs about four times a day while
    the feed is off, with the same retention as a running feed (7 days at the cap). Local
    only, no network; 0 and no file created when there is no store; never raises.
    """
    try:
        target = Path(store_path) if store_path is not None else default_store_path()
        if not target.is_file():
            return 0
        store = Store(target, busy_ms=IDLE_BUSY_MS)
    except Exception as exc:  # noqa: BLE001 - retention must never break the idle path
        log.warning("j7_feed: retention could not open the idle store (%s)", type(exc).__name__)
        return 0
    try:
        retention = CAPACITY_RETENTION_MS if store.check_capacity() else RETENTION_MS
        removed = await store.prune((clock or now_ms)(), retention)
        if removed:
            log.info("j7_feed: retention removed %d rows older than %d days (feed idle)", removed,
                     retention // 86_400_000)
        return removed
    except Exception as exc:  # noqa: BLE001
        log.warning("j7_feed: retention on the idle store failed (%s)", type(exc).__name__)
        return 0
    finally:
        store.close()


async def _idle(code: str, store_path: Path | str | None, clock: Callable[[], int]) -> dict[str, Any]:
    await prune_idle_store(store_path, clock=clock)
    return {"idle": code}


# ------------------------------------------------------------------------------- read API


@contextlib.contextmanager
def _reader(path: Path | str | None) -> Iterator[sqlite3.Connection | None]:
    """A read-only, query-only connection with a 5 s deadline, closed on exit; ``None`` when
    the store does not exist yet. Never a long read: every query here is indexed and limited."""
    target = Path(path) if path is not None else default_store_path()
    if not target.exists():
        yield None
        return
    conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        deadline = time.monotonic() + READ_DEADLINE_S
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        yield conn
    finally:
        conn.close()


def normalize_filters(*, handle: Any = None, token: Any = None, chain: Any = None, minutes: Any = 60,
                      limit: Any = 30) -> dict[str, Any]:
    """Validated read filters; ``ValueError`` names the bad one. ``minutes`` is clamped to the
    14-day retention and ``limit`` to :data:`READ_MAX_ROWS`."""
    out: dict[str, Any] = {"handle": None, "token": None, "chain": None}
    if handle not in (None, ""):
        out["handle"] = normalize_handle(handle)
        if out["handle"] is None:
            raise ValueError("handle must be an account handle such as 'elonmusk'")
    if token not in (None, ""):
        value = _text(token, 64)
        if value and looks_evm(value):
            out["token"] = value.lower()
        elif value and looks_solana(value):
            out["token"] = value
        else:
            raise ValueError("token must be an exact Solana mint or EVM contract address")
    if chain not in (None, ""):
        hint = _chain_hint(chain)
        if hint is None:
            raise ValueError("chain must be a chain name such as 'sol', 'bsc' or 'base'")
        out["chain"] = CHAIN_ALIASES.get(_collapse(hint), hint)
    out["minutes"] = max(1, min(int(minutes), READ_MAX_MINUTES))
    out["limit"] = max(1, min(int(limit), READ_MAX_ROWS))
    return out


def _latest_meta(conn: sqlite3.Connection, address: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT symbol, name, market, mcap_usd, liquidity_usd, is_honeypot, observed_ms FROM token_meta "
        "WHERE address = ? ORDER BY observed_ms DESC LIMIT 1", (address,),
    ).fetchone()
    if row is None:
        return None
    meta = dict(row)
    meta["is_honeypot"] = None if meta["is_honeypot"] is None else bool(meta["is_honeypot"])
    return meta


def recent_posts(*, handle: Any = None, token: Any = None, chain: Any = None, minutes: Any = 60,
                 limit: Any = 30, contracts_only: bool = False, path: Path | str | None = None,
                 now_ms: int | None = None, text_chars: int = 500) -> list[dict[str, Any]] | None:
    """Recent recorded posts, newest first seen first, each with its mentions and J7's latest
    token metadata per address. ``None`` when nothing has been recorded yet (no store file).

    The text is third-party content, cut to ``text_chars``; callers that hand it to a model
    must treat it as data, never as instructions.
    """
    f = normalize_filters(handle=handle, token=token, chain=chain, minutes=minutes, limit=limit)
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    where = ["p.first_seen_ms >= ?"]
    params: list[Any] = [now - f["minutes"] * 60_000]
    if f["handle"]:
        where.append("p.author_handle = ?")
        params.append(f["handle"])
    if f["token"]:
        sub = "SELECT m.platform, m.post_id FROM mentions m WHERE m.address = ?"
        params.append(f["token"])
        if f["chain"]:
            sub += " AND m.chain = ?"
            params.append(f["chain"])
        where.append(f"(p.platform, p.post_id) IN ({sub})")
    elif f["chain"] or contracts_only:
        sub = "SELECT 1 FROM mentions m WHERE m.platform = p.platform AND m.post_id = p.post_id"
        if f["chain"]:
            sub += " AND m.chain = ?"
            params.append(f["chain"])
        where.append(f"EXISTS ({sub})")
    params.append(f["limit"])
    sql = ("SELECT p.platform, p.post_id, p.author_handle, p.author_id, p.author_name, p.kind, p.text, "
           "p.created_ms, p.first_seen_ms, p.updated_ms, p.deleted_ms, p.initial, p.quoted_id, "
           "p.reply_to_id, p.media_count, p.metrics, p.coin_pairs FROM posts p "
           f"WHERE {' AND '.join(where)} ORDER BY p.first_seen_ms DESC, p.id DESC LIMIT ?")
    with _reader(path) as conn:
        if conn is None:
            return None
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        by_platform: dict[str, list[str]] = {}
        for row in rows:
            by_platform.setdefault(row["platform"], []).append(row["post_id"])
        mentions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for platform, ids in by_platform.items():
            marks = ",".join("?" for _ in ids)
            for m in conn.execute(
                "SELECT platform, post_id, chain, address, chain_hint, source, first_seen_ms FROM mentions "
                f"WHERE platform = ? AND post_id IN ({marks}) ORDER BY first_seen_ms, id",
                (platform, *ids),
            ).fetchall():
                mentions.setdefault((m["platform"], m["post_id"]), []).append(dict(m))
        metas: dict[str, dict[str, Any] | None] = {}
        out: list[dict[str, Any]] = []
        for row in rows:
            text = row["text"] or ""
            post_mentions = []
            for m in mentions.get((row["platform"], row["post_id"]), [])[:8]:
                address = m["address"]
                if address not in metas and len(metas) < 100:
                    metas[address] = _latest_meta(conn, address)
                post_mentions.append({
                    "chain": m["chain"], "address": address, "chain_hint": m["chain_hint"],
                    "source": m["source"], "first_seen_ms": m["first_seen_ms"], "token": metas.get(address),
                })
            handle_ = row["author_handle"]
            url = (f"https://x.com/{handle_}/status/{row['post_id']}"
                   if row["platform"] == "x" and handle_ and str(row["post_id"]).isdigit() else None)
            out.append({
                "platform": row["platform"], "post_id": row["post_id"], "url": url,
                "author": handle_, "author_name": row["author_name"], "author_id": row["author_id"],
                "kind": row["kind"], "text": text[:text_chars] or None,
                "text_truncated": len(text) > text_chars, "created_ms": row["created_ms"],
                "first_seen_ms": row["first_seen_ms"], "updated_ms": row["updated_ms"],
                "deleted": row["deleted_ms"] is not None, "deleted_ms": row["deleted_ms"],
                "backlog": bool(row["initial"]), "quoted_id": row["quoted_id"],
                "reply_to_id": row["reply_to_id"], "media_count": row["media_count"],
                "metrics": json.loads(row["metrics"]) if row["metrics"] else None,
                "coin_pairs": row["coin_pairs"].split(",") if row["coin_pairs"] else [],
                "mentions": post_mentions,
            })
        return out


def token_expires_ms(*, path: Path | str | None = None) -> int | None:
    """The session token's ``exp`` (epoch ms) as the listener last noted it; ``None`` when it
    is unknown (no store yet, an opaque token, no ``exp``, or the token is gone)."""
    with _reader(path) as conn:
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT value FROM feed_state WHERE key = 'token_expires_ms'").fetchone()
        except sqlite3.Error:  # a store from before feed_state existed
            return None
    value = row["value"] if row is not None else None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def feed_summary(*, minutes: Any = 60, path: Path | str | None = None,
                 now_ms: int | None = None) -> dict[str, Any] | None:
    """Counts over the window, the most-mentioned addresses, J7's last upstream status and
    the unknown-event counters. ``None`` when nothing has been recorded yet."""
    window = max(1, min(int(minutes), READ_MAX_MINUTES))
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    since = now - window * 60_000
    target = Path(path) if path is not None else default_store_path()
    with _reader(target) as conn:
        if conn is None:
            return None
        totals = dict(conn.execute(
            "SELECT COUNT(*) AS posts, COALESCE(SUM(initial = 0), 0) AS live_posts, "
            "COALESCE(SUM(deleted_ms IS NOT NULL), 0) AS deleted, COALESCE(SUM(ca IS NOT NULL), 0) AS with_contract, "
            "COUNT(DISTINCT author_handle) AS authors FROM posts WHERE first_seen_ms >= ?", (since,),
        ).fetchone())
        platforms = {r["platform"]: r["n"] for r in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM posts WHERE first_seen_ms >= ? GROUP BY platform "
            "ORDER BY n DESC LIMIT 10", (since,),
        ).fetchall()}
        top = []
        for r in conn.execute(
            "SELECT chain, address, COUNT(*) AS posts, MIN(first_seen_ms) AS first_seen_ms FROM mentions "
            "WHERE first_seen_ms >= ? GROUP BY chain, address ORDER BY posts DESC, first_seen_ms DESC LIMIT 10",
            (since,),
        ).fetchall():
            meta = _latest_meta(conn, r["address"])
            top.append({**dict(r), "symbol": meta["symbol"] if meta else None})
        last = conn.execute(
            "SELECT first_seen_ms FROM posts WHERE initial = 0 ORDER BY first_seen_ms DESC LIMIT 1"
        ).fetchone()
        status = conn.execute(
            "SELECT payload, observed_ms FROM j7_events WHERE event = 'status' ORDER BY observed_ms DESC LIMIT 1"
        ).fetchone()
        counters = {r["name"]: r["n"] for r in conn.execute(
            "SELECT name, n FROM counters ORDER BY n DESC LIMIT 20"
        ).fetchall()}
    upstream = None
    if status is not None:
        with contextlib.suppress(TypeError, ValueError):
            upstream = {"connected": json.loads(status["payload"]).get("connected"),
                        "observed_ms": status["observed_ms"]}
    size = sum(p.stat().st_size for p in target.parent.glob(target.name + "*") if p.is_file())
    return {
        "window_minutes": window, **totals, "by_platform": platforms, "top_mentions": top,
        "last_live_post_ms": last["first_seen_ms"] if last else None, "upstream_x_feed": upstream,
        "counters": counters, "store_bytes": size, "store_cap_bytes": CAP_BYTES,
    }
