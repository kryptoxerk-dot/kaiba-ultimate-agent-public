"""Copycat detection: is this launch the original, or is it a copy of one?

This is the highest-evidence pre-trade variable in the whole research review
(``docs/EDGE-AND-VARIABLES.md`` §1, variable #1) and the cheapest. Measured over 15.2M
pump.fun coins at CCS'26 (arXiv 2609.10246, "Meme Coin Factories"): **originals graduate
at 9.20%, copycats at 0.86%** — a 10.7x separation for the cost of one hash comparison.
More than 1.5M coins, over 10% of all launches, duplicate an existing name, symbol,
description or image.

Three ideas carry the whole module.

**Originality is a timestamp question, not a similarity question.** Two coins sharing an
image is not interesting; which of them used it *first* is the entire signal. So the
registry stores, per fingerprint, the earliest mint that carried it, and a backfill that
arrives out of order demotes the incumbent rather than cementing the wrong original.

**Unknown is a real answer.** If we have never seen a fingerprint, that is only evidence
of originality when we were already watching before the token was born. Before that point
the honest verdict is UNKNOWN, and ``dedup_coverage`` is what makes the difference
decidable. A system that returns ORIGINAL for everything it has not indexed is worse than
one that returns nothing, because it manufactures confidence out of a cold cache.

**The cheapest fingerprint is the strongest.** An IPFS CID *is* a content hash, so a
reused CID proves byte-identical media with no download at all. Text fingerprints are
free too. Only the perceptual hash needs bytes on the wire, and it is therefore opt-in
(``with_image=True``): it is the one that catches a re-encode, which is the common
evasion, but at 150 KB median per image it is not something to do on the hot path for
every one of 14 launches a minute.

**Measured here, 2026-09-20, on 1,038 consecutive pump.fun launches spanning 1.26 hours
(a contiguous page-walk of the launch feed, so the window is complete):**

====================================  ======  ======================================
criterion                             rate    note
====================================  ======  ======================================
name, symbol, description or image    46.4%   the published paper's four fields
same four fields, raw strings, no     46.4%   so our normalisation adds +0.09pp, and
normalisation at all                          is *not* what inflates the number
image CID or metadata CID only        17.8%   content hash: proof of identical bytes
name only                             38.1%
symbol only                           42.8%
description only (watermarks removed)  3.8%   was 17% before the watermark fix
====================================  ======  ======================================

**That is 4.5x the 10.2% the paper reports, and the gap is not a matching bug.** An exact
unnormalised match gives the same answer, and the largest clusters are verifiable: one
creator wallet, one image CID, twenty mints, twelve different tickers between them (that
cluster is recorded in ``tests/fixtures/dedup/copycat_cluster.json``). The 2026 launch
stream is simply far more duplicative than the historical corpus the paper measured, so
**the 9.20% / 0.86% graduation split must be re-measured before it is trusted here** —
``docs/EDGE-AND-VARIABLES.md`` §4 item 17 says to discard every pre-2026 threshold, and
this is one. Until that forward test exists, treat :attr:`Verdict.proof_match` (17.8%,
content-hash only) as the defensible filter and a text-only match as a warning.

**On the image hashing.** Pillow is not installed and this is not worth a dependency, so
there is a small PNG decoder and a DC-only JPEG decoder in here. The JPEG one exploits the
fact that a baseline JPEG's DC coefficients already *are* a 1/8-scale thumbnail: no IDCT,
no chroma, no colour conversion. Both were cross-checked against ffmpeg's own scaler on
real launch images and agreed to within 1-4 bits of 64.

**WebP is the hole**, and decoding it needs a VP8 implementation this is not going to
contain. Those mints get the exact hash and an explicit ``unsupported:webp`` status
recorded per mint, so byte-identical reuse and CID reuse are still caught for them and
only the *re-encode* evasion is missed.

Measured on 250 consecutive launches with ``with_image=True``, 2026-09-20:

====================================  =========================================
image fetch success                   100% (250/250, via pump's Pinata gateway)
decoded and perceptually hashed       71.6%
WebP, exact hash only                 27.2%
too flat to hash (low entropy)         1.2%
cost per coin                         1,867 ms, ~300 KB
exact-hash matches                    62      <- the download's real payoff
CID matches (free, no download)       23
perceptual matches                    14, of which 3 were not exact
**perceptual-only catches**           **3 of 250 = 1.2%**
====================================  =========================================

Read the last two rows honestly: the image *download* is worth it (the exact hash caught
2.7x what the free CID did, because a quarter of launches serve images off a plain CDN
with no CID at all), but the **perceptual hash — the most complex code in this file —
bought three extra detections out of 250.** It exists to catch deliberate re-encode
evasion, which is rare in the general stream and is exactly what someone does when they
know a de-duplicator is watching. That is a defensible reason to keep it and a bad reason
to put it on the hot path.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
import unicodedata
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from kaiba.core.db import fetch_all, fetch_one, get_conn, tx
from kaiba.core.limiter import Priority, RateLimited, guarded
from kaiba.core.schemas import Chain, EvidenceBasis, Receipt, digest, now_ms
from kaiba.intelligence.hubs import safe_normalize
from kaiba.providers._http import cache_read, cache_write, get_json, redact_text

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------- constants

#: Free, unauthenticated, verified working 2026-09-20. Returns name, symbol, description,
#: image_uri, metadata_uri and created_timestamp for any pump.fun mint.
PUMPFUN_COIN_URL = "https://frontend-api-v3.pump.fun/coins/{mint}"
PUMPFUN_LIST_URL = "https://frontend-api-v3.pump.fun/coins"

#: pump.fun serves ``image_uri`` as ``https://ipfs.io/ipfs/<cid>``, and ipfs.io answers us
#: with ``403 blocked`` after the first request. pump's own Pinata gateway answers every
#: time. Ordered by measured reliability; the CID is the same on all of them.
IPFS_GATEWAYS: tuple[str, ...] = (
    "https://pump.mypinata.cloud/ipfs/{cid}",
    "https://gateway.pinata.cloud/ipfs/{cid}",
    "https://ipfs.io/ipfs/{cid}",
)
ARWEAVE_GATEWAY = "https://arweave.net/{cid}"

#: A browser user agent. pump.fun's edge is less generous to ``python-httpx/0.28``.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 KAIBA/0.1"

#: Coin metadata is immutable once launched (the bonding-curve numbers in the same payload
#: are not, but we do not read them), so cache it for a week.
COIN_TTL_S = 7 * 86_400
IMAGE_TTL_S = 30 * 86_400

MAX_IMAGE_BYTES = 8 * 1024 * 1024
#: Refuse to decode anything larger. Pure-Python PNG unfiltering is O(bytes) and a
#: 4000x4000 image would take seconds; the median pump.fun image is 148 KB.
MAX_PIXELS = 4_000_000
#: Longest axis of the intermediate greyscale sample. The hash grid is 9x8, so anything
#: past this buys nothing and costs a Python loop per pixel.
SAMPLE_MAX = 128

#: dHash side. 8 gives a 9x8 greyscale grid and 64 bits of horizontal-gradient comparison.
DHASH_SIDE = 8
#: Bits per band in ``dedup_image_bands``. Eight 8-bit bands means the pigeonhole
#: principle guarantees exhaustive recall for Hamming distance <= 7.
BAND_BITS = 8
BAND_COUNT = (DHASH_SIDE * DHASH_SIDE) // BAND_BITS
#: Default near-duplicate threshold. Must stay <= BAND_COUNT - 1 or recall stops being
#: exhaustive and the band index starts silently missing matches.
#:
#: Measured on real pump.fun images against ffmpeg-produced variants (the numbers behind
#: this constant, reproducible with the fixtures in ``tests/fixtures/dedup/``):
#:   JPEG re-encode at any quality  0-2 bits    caught
#:   brightness +15%                0-2 bits    caught
#:   contrast x1.3                  0-3 bits    caught
#:   50% downscale                  1-9 bits    caught about half the time
#:   4% centre crop                 1-8 bits    caught about half the time
#:   horizontal flip                11-35 bits  NOT caught, and dHash never will
DEFAULT_DHASH_DISTANCE = 6

#: A dHash with almost no bits set comes from a nearly flat image — a white square with a
#: word on it, which is a large fraction of memecoin art. Such hashes sit close to each
#: other by construction, so registering them would generate matches between unrelated
#: tokens. Below this popcount the perceptual fingerprint is dropped and only the exact
#: hash survives; the mint's image status records that it happened.
MIN_DHASH_POPCOUNT = 8

#: Below these lengths a text fingerprint is noise, not identity. Three-character names
#: and one-character symbols collide constantly between unrelated launches.
MIN_NAME_LEN = 3
MIN_SYMBOL_LEN = 2
MIN_DESCRIPTION_LEN = 16


class FingerprintKind(StrEnum):
    """What was matched. Ordered loosely by how much a match is worth."""

    IMAGE_SHA256 = "image_sha256"
    IMAGE_CID = "image_cid"
    METADATA_CID = "metadata_cid"
    IMAGE_DHASH = "image_dhash"
    DESCRIPTION = "description"
    NAME = "name"
    SYMBOL = "symbol"


#: Strength ranking, highest first. A shared content hash or CID proves identical bytes.
#: A shared symbol proves almost nothing on its own — thousands of unrelated coins are
#: called "DOGE" — so it ranks last and the caller can see which kind fired.
KIND_STRENGTH: dict[FingerprintKind, int] = {
    FingerprintKind.IMAGE_SHA256: 100,
    FingerprintKind.IMAGE_CID: 95,
    FingerprintKind.METADATA_CID: 90,
    FingerprintKind.IMAGE_DHASH: 80,
    FingerprintKind.DESCRIPTION: 60,
    FingerprintKind.NAME: 40,
    FingerprintKind.SYMBOL: 20,
}


#: Kinds where a match is *proof* of identical bytes rather than identical words. A CID
#: and a SHA-256 are both content hashes, so a collision is not a coincidence. Text
#: matches are evidence; these are arithmetic. Downstream sizing should be able to tell
#: the two apart, so the verdict exposes both.
PROOF_KINDS: frozenset[FingerprintKind] = frozenset(
    {FingerprintKind.IMAGE_SHA256, FingerprintKind.IMAGE_CID, FingerprintKind.METADATA_CID}
)


class Status(StrEnum):
    """The three answers. ``UNKNOWN`` is a first-class outcome, not an error code."""

    ORIGINAL = "original"
    COPYCAT = "copycat"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Fingerprint:
    kind: FingerprintKind
    value: str


@dataclass(frozen=True, slots=True)
class Match:
    """A fingerprint this mint shares with an older one."""

    kind: FingerprintKind
    value: str
    original_mint: str
    original_created_ms: int | None
    original_first_seen_ms: int
    age_gap_ms: int | None
    #: Hamming distance for a perceptual match, 0 for an exact one, ``None`` when the
    #: kind has no notion of distance.
    distance: int | None = None
    #: True when the *other* mint is the copy and we are the older one. A collision is
    #: symmetric; which side is the copycat is not, and conflating the two would label
    #: the seed of a factory cluster a copy of its own output whenever a backfill
    #: happened to reach the copies first.
    copy_of_us: bool = False

    @property
    def age_gap_s(self) -> float | None:
        return None if self.age_gap_ms is None else self.age_gap_ms / 1000.0

    @property
    def proof(self) -> bool:
        """True when the matched fingerprint is a content hash, not a string."""
        return self.kind in PROOF_KINDS


@dataclass(frozen=True, slots=True)
class TokenMeta:
    """The immutable identity fields of a launch."""

    mint: str
    chain: Chain
    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image_uri: str | None = None
    metadata_uri: str | None = None
    created_ms: int | None = None
    creator: str | None = None

    @property
    def empty(self) -> bool:
        return not any((self.name, self.symbol, self.description, self.image_uri, self.metadata_uri))


@dataclass(frozen=True)
class Verdict:
    """The answer, with everything needed to argue with it."""

    mint: str
    chain: Chain
    status: Status
    basis: EvidenceBasis
    #: Fingerprints an *older* mint already had. Non-empty means COPYCAT.
    matches: tuple[Match, ...] = ()
    #: Fingerprints *newer* mints took from this one. Evidence that this mint is the seed
    #: of a cluster, not a member of one — the opposite reading, and worth surfacing:
    #: a launch whose image is already being cloned is the original of a factory run.
    copied_by: tuple[Match, ...] = ()
    fingerprints: tuple[Fingerprint, ...] = ()
    created_ms: int | None = None
    image_status: str | None = None
    coverage_start_ms: int | None = None
    receipts: tuple[Receipt, ...] = ()
    unknowns: tuple[str, ...] = ()
    note: str | None = None

    @property
    def best(self) -> Match | None:
        """The strongest match, which is the one a caller acting on one number wants."""
        if not self.matches:
            return None
        return max(self.matches, key=lambda m: (KIND_STRENGTH.get(m.kind, 0), -(m.distance or 0)))

    @property
    def proof_match(self) -> Match | None:
        """The strongest content-hash match, if any. Byte-identical media, provably."""
        proofs = [m for m in self.matches if m.proof]
        if not proofs:
            return None
        return max(proofs, key=lambda m: KIND_STRENGTH.get(m.kind, 0))

    @property
    def is_copycat(self) -> bool:
        return self.status is Status.COPYCAT

    @property
    def known(self) -> bool:
        return self.status is not Status.UNKNOWN

    def summary(self) -> str:
        best = self.best
        if best is None:
            return f"{self.status.value}"
        gap = "" if best.age_gap_s is None else f", {best.age_gap_s / 3600:.1f}h older"
        return f"{self.status.value} via {best.kind.value} of {best.original_mint}{gap}"


# --------------------------------------------------------------------- text fingerprints

#: Format characters (Cf) cover most of it, but these are the ones actually used to defeat
#: naive matching and a couple are not Cf.
_ZERO_WIDTH = "".join(
    ("​", "‌", "‍", "⁠", "﻿", "­", "͏", "᠎")
)
_ZERO_WIDTH_RE = re.compile(f"[{re.escape(_ZERO_WIDTH)}]")

#: Cyrillic and Greek lookalikes. A homoglyph swap is a one-keystroke evasion of an exact
#: text match and costs us nothing to undo. Deliberately small: only the characters whose
#: Latin reading is unambiguous.
_HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i",
        "ѕ": "s", "ј": "j", "к": "k", "м": "m", "н": "h", "т": "t", "в": "b", "ԁ": "d",
        "ɡ": "g", "ν": "v", "ο": "o", "ρ": "p", "α": "a", "ε": "e", "τ": "t", "υ": "u",
        "ι": "i", "κ": "k", "μ": "m", "χ": "x", "ϲ": "c", "һ": "h", "ӏ": "l", "ᴏ": "o",
    }
)

_WS_RE = re.compile(r"\s+")

#: Deployer watermarks. Measured on 1,038 consecutive pump.fun launches: the three most
#: duplicated descriptions were "Launched on discord.gg/uxento" (65 coins, 7 creators),
#: "Created on https://rapidlaunch.io" (45 coins, 11 creators) and "Deployed using
#: https://j7tracker.io" (34 coins, 25 creators) — 80% of all repeated descriptions. That
#: is the launch tool signing its own work, not a copycat, and counting it inflated the
#: measured copycat rate by roughly 15 percentage points. Stripping the link rather than
#: keeping a stoplist of tools is the version that still works when the next tool appears:
#: what is left of "Created on <url>" is too short to be a description at all.
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.I)
_BARE_DOMAIN_RE = re.compile(
    r"\b[a-z0-9][a-z0-9-]*\.(?:io|com|gg|fun|xyz|net|org|app|co|ai|so|sh|link|me|tv|lol)\b(?:/\S*)?",
    re.I,
)


def normalize_text(value: str | None) -> str:
    """Fold away everything that is decoration rather than identity.

    NFKC, zero-width removal, homoglyph folding, case folding, symbol and emoji removal,
    whitespace collapse. The order matters: NFKC first so that fullwidth and ligature
    forms become their ASCII equivalents before anything else looks at them.
    """
    if not value:
        return ""
    s = unicodedata.normalize("NFKC", value)
    s = _ZERO_WIDTH_RE.sub("", s)
    s = s.translate(_HOMOGLYPHS)
    s = s.casefold()
    # Drop emoji, pictographs, modifier symbols and every remaining format character.
    # "🚀🚀 DOGE 🚀🚀" and "DOGE" are the same name wearing different hats.
    out: list[str] = []
    for ch in s:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cf", "Cc", "Co", "Cs"):
            out.append(" ")
            continue
        out.append(ch)
    return _WS_RE.sub(" ", "".join(out)).strip()


def normalize_description(value: str | None) -> str:
    """:func:`normalize_text` with links removed first.

    A description whose only content was a link to the tool that deployed the coin has
    nothing left afterwards and therefore produces no fingerprint, which is the correct
    outcome: it identifies the launchpad, not the token.
    """
    if not value:
        return ""
    stripped = _BARE_DOMAIN_RE.sub(" ", _URL_RE.sub(" ", value))
    return normalize_text(stripped)


def normalize_symbol(value: str | None) -> str:
    """As :func:`normalize_text`, then strip to alphanumerics.

    Tickers are written ``$DOGE``, ``DOGE``, ``D.O.G.E`` and ``ＤＯＧＥ``; none of those
    differences are a different token.
    """
    base = normalize_text(value)
    return re.sub(r"[^0-9a-z]+", "", base)


def text_fingerprints(meta: TokenMeta) -> list[Fingerprint]:
    """Name, symbol and description fingerprints, skipping the degenerate ones."""
    out: list[Fingerprint] = []
    name = normalize_text(meta.name)
    if len(name) >= MIN_NAME_LEN:
        out.append(Fingerprint(FingerprintKind.NAME, name))
    symbol = normalize_symbol(meta.symbol)
    if len(symbol) >= MIN_SYMBOL_LEN:
        out.append(Fingerprint(FingerprintKind.SYMBOL, symbol))
    desc = normalize_description(meta.description)
    # A description that is just the name or the ticker is not an independent fingerprint;
    # counting it would double-weight a single weak match.
    if len(desc) >= MIN_DESCRIPTION_LEN and desc != name and desc.replace(" ", "") != symbol:
        out.append(Fingerprint(FingerprintKind.DESCRIPTION, _hash_text(desc)))
    return out


def _hash_text(value: str) -> str:
    """Descriptions can be kilobytes; store the digest, not the essay."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------- content ids

#: CIDv0 (Qm...) and CIDv1 (bafy.../bafk...), plus a bare 43-char Arweave transaction id.
_CIDV0_RE = re.compile(r"\bQm[1-9A-HJ-NP-Za-km-z]{44}\b")
_CIDV1_RE = re.compile(r"\bb[a-z2-7]{58,}\b")
_ARWEAVE_RE = re.compile(r"\b[A-Za-z0-9_-]{43}\b")


def content_id(uri: str | None) -> str | None:
    """The content-addressed id inside a URI, or ``None`` when there is not one.

    A CID is itself a hash of the bytes, so two launches sharing one are provably sharing
    the file — the strongest available signal, obtainable without downloading anything.
    A plain CDN URL is not content addressed and gets no fingerprint: two tokens served
    the same ``.../logo.png`` path from different accounts are not the same image.
    """
    if not uri:
        return None
    text = uri.strip()
    if not text:
        return None
    m = _CIDV0_RE.search(text)
    if m:
        return m.group(0)
    m = _CIDV1_RE.search(text)
    if m:
        return m.group(0)
    if "arweave.net" in text or text.startswith("ar://"):
        m = _ARWEAVE_RE.search(text.rsplit("/", 1)[-1])
        if m:
            return f"ar:{m.group(0)}"
    return None


def uri_fingerprints(meta: TokenMeta) -> list[Fingerprint]:
    out: list[Fingerprint] = []
    image_cid = content_id(meta.image_uri)
    if image_cid:
        out.append(Fingerprint(FingerprintKind.IMAGE_CID, image_cid))
    meta_cid = content_id(meta.metadata_uri)
    if meta_cid:
        out.append(Fingerprint(FingerprintKind.METADATA_CID, meta_cid))
    return out


# ------------------------------------------------------------------------ image decoding


class DecodeError(Exception):
    """An image we cannot turn into pixels. Never propagates out of this module."""


@dataclass(frozen=True, slots=True)
class Grey:
    """A small greyscale raster, row-major, one byte per pixel."""

    width: int
    height: int
    pixels: bytes


def image_format(data: bytes) -> str:
    """Sniff by magic bytes. Content-Type headers from IPFS gateways lie routinely."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"<svg" or data[:5] == b"<?xml":
        return "svg"
    return "unknown"


# --- PNG -------------------------------------------------------------------------------


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(ftype: int, line: bytearray, prev: bytes, bpp: int) -> None:
    """In-place PNG unfiltering of one scanline. The hot loop of PNG decoding."""
    if ftype == 0:
        return
    n = len(line)
    if ftype == 1:  # Sub
        for i in range(bpp, n):
            line[i] = (line[i] + line[i - bpp]) & 0xFF
    elif ftype == 2:  # Up
        for i in range(n):
            line[i] = (line[i] + prev[i]) & 0xFF
    elif ftype == 3:  # Average
        for i in range(n):
            left = line[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:  # Paeth
        for i in range(n):
            left = line[i - bpp] if i >= bpp else 0
            upleft = prev[i - bpp] if i >= bpp else 0
            line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
    else:
        raise DecodeError(f"png filter {ftype}")


def _png_grey(data: bytes) -> Grey:
    """Decode a non-interlaced PNG to a sampled greyscale raster.

    Sampled rather than full-size because the caller only ever wants a 9x8 grid: every
    scanline must be unfiltered (the filters are sequential) but only ``SAMPLE_MAX``
    of them need to become pixels.
    """
    pos = 8
    width = height = depth = colour = interlace = 0
    palette = b""
    idat: list[bytes] = []
    n = len(data)
    while pos + 8 <= n:
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            if len(body) < 13:
                raise DecodeError("truncated IHDR")
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            depth, colour, interlace = body[8], body[9], body[12]
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
    if not width or not height:
        raise DecodeError("no IHDR")
    if interlace:
        raise DecodeError("interlaced png")
    if width * height > MAX_PIXELS:
        raise DecodeError(f"png too large ({width}x{height})")
    if not idat:
        raise DecodeError("no IDAT")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    if channels is None:
        raise DecodeError(f"png colour type {colour}")
    if depth not in (1, 2, 4, 8, 16):
        raise DecodeError(f"png bit depth {depth}")
    if colour == 3 and not palette:
        raise DecodeError("palette png without PLTE")

    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise DecodeError(f"png inflate: {exc}") from exc

    bits_per_pixel = channels * depth
    bpp = max(1, bits_per_pixel // 8)
    stride = (width * bits_per_pixel + 7) // 8
    if len(raw) < (stride + 1) * height:
        raise DecodeError("png short scanlines")

    out_w = min(width, SAMPLE_MAX)
    out_h = min(height, SAMPLE_MAX)
    cols = [x * width // out_w for x in range(out_w)]
    wanted = {y * height // out_h: y for y in range(out_h)}

    pixels = bytearray(out_w * out_h)
    prev = bytes(stride)
    off = 0
    for y in range(height):
        ftype = raw[off]
        line = bytearray(raw[off + 1 : off + 1 + stride])
        off += 1 + stride
        _unfilter(ftype, line, prev, bpp)
        prev = bytes(line)
        slot = wanted.get(y)
        if slot is None:
            continue
        base = slot * out_w
        for i, x in enumerate(cols):
            pixels[base + i] = _png_pixel_grey(line, x, depth, colour, channels, palette)
    return Grey(out_w, out_h, bytes(pixels))


def _sample_channel(line: bytearray, index: int, depth: int) -> int:
    """One sub-pixel sample scaled to 0-255, for any PNG bit depth."""
    if depth == 8:
        return line[index]
    if depth == 16:
        return line[index * 2]
    per_byte = 8 // depth
    byte = line[index // per_byte]
    shift = 8 - depth * (index % per_byte + 1)
    value = (byte >> shift) & ((1 << depth) - 1)
    return value * 255 // ((1 << depth) - 1)


def _png_pixel_grey(
    line: bytearray, x: int, depth: int, colour: int, channels: int, palette: bytes
) -> int:
    if colour == 3:
        per_byte = 8 // depth if depth < 8 else 1
        if depth < 8:
            byte = line[x // per_byte]
            shift = 8 - depth * (x % per_byte + 1)
            idx = (byte >> shift) & ((1 << depth) - 1)
        else:
            idx = line[x]
        base = idx * 3
        if base + 2 >= len(palette):
            return 0
        r, g, b = palette[base], palette[base + 1], palette[base + 2]
        return (77 * r + 150 * g + 29 * b) >> 8
    first = x * channels
    if colour in (0, 4):
        return _sample_channel(line, first, depth)
    r = _sample_channel(line, first, depth)
    g = _sample_channel(line, first + 1, depth)
    b = _sample_channel(line, first + 2, depth)
    return (77 * r + 150 * g + 29 * b) >> 8


# --- JPEG ------------------------------------------------------------------------------


class _Bits:
    """Entropy-coded bit reader with 0xFF00 unstuffing and a 16-bit peek window."""

    __slots__ = ("data", "pos", "buf", "nbits", "hit_marker")

    def __init__(self, data: bytes, pos: int) -> None:
        self.data = data
        self.pos = pos
        self.buf = 0
        self.nbits = 0
        self.hit_marker = False

    def _fill(self) -> None:
        data, n = self.data, len(self.data)
        while self.nbits <= 48:
            if self.hit_marker or self.pos >= n:
                self.buf = (self.buf << 8) & ((1 << 64) - 1)
                self.nbits += 8
                continue
            byte = data[self.pos]
            self.pos += 1
            if byte == 0xFF:
                nxt = data[self.pos] if self.pos < n else 0xD9
                if nxt == 0x00:
                    self.pos += 1
                elif 0xD0 <= nxt <= 0xD7:
                    # A restart marker terminates the current run; reset_to_marker eats it.
                    self.pos -= 1
                    self.hit_marker = True
                    byte = 0
                else:
                    self.pos -= 1
                    self.hit_marker = True
                    byte = 0
            self.buf = ((self.buf << 8) | byte) & ((1 << 64) - 1)
            self.nbits += 8

    def peek16(self) -> int:
        if self.nbits < 16:
            self._fill()
        return (self.buf >> (self.nbits - 16)) & 0xFFFF

    def drop(self, count: int) -> None:
        if self.nbits < count:
            self._fill()
        self.nbits -= count

    def receive(self, count: int) -> int:
        if count <= 0:
            return 0
        if self.nbits < count:
            self._fill()
        self.nbits -= count
        return (self.buf >> self.nbits) & ((1 << count) - 1)

    def align_and_skip_rst(self) -> bool:
        """Consume a restart marker if one is next. Returns True when it was."""
        self.buf = 0
        self.nbits = 0
        data, n = self.data, len(self.data)
        pos = self.pos
        while pos + 1 < n and data[pos] != 0xFF:
            pos += 1
        if pos + 1 < n and data[pos] == 0xFF and 0xD0 <= data[pos + 1] <= 0xD7:
            self.pos = pos + 2
            self.hit_marker = False
            return True
        return False


def _huff_table(counts: Sequence[int], symbols: Sequence[int]) -> list[tuple[int, int]]:
    """Flat 16-bit lookup: ``table[next16bits] -> (symbol, code length)``.

    Bit-at-a-time Huffman decoding costs one Python loop iteration per *bit*, which for a
    150 KB JPEG is well over a million iterations and several seconds. This costs one list
    index per *symbol* instead, and the table build is 65536 slice-assigned slots.
    """
    table: list[tuple[int, int]] = [(-1, 16)] * 65536
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if k >= len(symbols):
                raise DecodeError("jpeg huffman table short")
            span = 1 << (16 - length)
            start = code << (16 - length)
            table[start : start + span] = [(symbols[k], length)] * span
            code += 1
            k += 1
        code <<= 1
    return table


def _decode_huff(bits: _Bits, table: list[tuple[int, int]]) -> int:
    symbol, length = table[bits.peek16()]
    if symbol < 0:
        raise DecodeError("jpeg bad huffman code")
    bits.drop(length)
    return symbol


def _extend(value: int, count: int) -> int:
    if count == 0:
        return 0
    return value if value >= (1 << (count - 1)) else value - (1 << count) + 1


@dataclass(slots=True)
class _Component:
    cid: int
    h: int
    v: int
    tq: int
    pred: int = 0
    dc_table: int = 0
    ac_table: int = 0


def _jpeg_grey(data: bytes) -> Grey:
    """Decode a JPEG's DC coefficients into a 1/8-scale luma thumbnail.

    A baseline JPEG stores, per 8x8 block, a DC coefficient that is the block's mean.
    Reading only those gives a correct W/8 x H/8 greyscale image for free — no IDCT, no
    chroma upsampling, no colour conversion — which is more than enough resolution for a
    9x8 perceptual hash. Progressive JPEGs are handled through their first (DC) scan,
    which carries the same information shifted by the successive-approximation low bit.
    """
    n = len(data)
    pos = 2
    quant: dict[int, list[int]] = {}
    dc_tables: dict[int, list[tuple[int, int]]] = {}
    ac_tables: dict[int, list[tuple[int, int]]] = {}
    comps: list[_Component] = []
    width = height = 0
    progressive = False
    restart_interval = 0

    while pos + 3 < n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        pos += 2
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9:
            break
        if pos + 2 > n:
            break
        seglen = int.from_bytes(data[pos : pos + 2], "big")
        body = data[pos + 2 : pos + seglen]
        if marker == 0xDB:  # DQT
            i = 0
            while i < len(body):
                pq, tq = body[i] >> 4, body[i] & 15
                i += 1
                if pq:
                    quant[tq] = [int.from_bytes(body[i + j * 2 : i + j * 2 + 2], "big") for j in range(64)]
                    i += 128
                else:
                    quant[tq] = list(body[i : i + 64])
                    i += 64
        elif marker == 0xC4:  # DHT
            i = 0
            while i + 17 <= len(body):
                tc, th = body[i] >> 4, body[i] & 15
                counts = list(body[i + 1 : i + 17])
                total = sum(counts)
                symbols = list(body[i + 17 : i + 17 + total])
                i += 17 + total
                table = _huff_table(counts, symbols)
                if tc == 0:
                    dc_tables[th] = table
                else:
                    ac_tables[th] = table
        elif marker in (0xC0, 0xC1, 0xC2):  # SOF0 / SOF1 / SOF2
            progressive = marker == 0xC2
            height = int.from_bytes(body[1:3], "big")
            width = int.from_bytes(body[3:5], "big")
            count = body[5]
            comps = [
                _Component(body[6 + i * 3], body[7 + i * 3] >> 4, body[7 + i * 3] & 15, body[8 + i * 3])
                for i in range(count)
            ]
        elif marker in (0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            raise DecodeError(f"jpeg mode {marker:#x} unsupported")
        elif marker == 0xDD:  # DRI
            restart_interval = int.from_bytes(body[0:2], "big")
        elif marker == 0xDA:  # SOS
            return _jpeg_scan(
                data, pos + seglen, body, comps, quant, dc_tables, ac_tables,
                width, height, progressive, restart_interval,
            )
        pos += seglen
    raise DecodeError("jpeg: no scan found")


def _jpeg_scan(
    data: bytes,
    entropy_at: int,
    header: bytes,
    comps: list[_Component],
    quant: dict[int, list[int]],
    dc_tables: dict[int, list[tuple[int, int]]],
    ac_tables: dict[int, list[tuple[int, int]]],
    width: int,
    height: int,
    progressive: bool,
    restart_interval: int,
) -> Grey:
    if not comps or not width or not height:
        raise DecodeError("jpeg: no frame")
    if width * height > MAX_PIXELS:
        raise DecodeError(f"jpeg too large ({width}x{height})")

    ns = header[0]
    scan: list[_Component] = []
    by_id = {c.cid: c for c in comps}
    for i in range(ns):
        cid = header[1 + i * 2]
        tables = header[2 + i * 2]
        comp = by_id.get(cid)
        if comp is None:
            raise DecodeError("jpeg: scan names an unknown component")
        comp.dc_table = tables >> 4
        comp.ac_table = tables & 15
        scan.append(comp)
    ss = header[1 + ns * 2]
    al = header[3 + ns * 2] & 15
    ah = header[3 + ns * 2] >> 4
    if progressive and not (ss == 0 and ah == 0):
        raise DecodeError("jpeg: progressive first scan is not DC")

    luma = comps[0]
    if luma not in scan:
        raise DecodeError("jpeg: luma missing from first scan")
    hmax = max(c.h for c in comps) or 1
    vmax = max(c.v for c in comps) or 1
    mcus_x = (width + 8 * hmax - 1) // (8 * hmax)
    mcus_y = (height + 8 * vmax - 1) // (8 * vmax)
    single = len(scan) == 1
    if single:
        blocks_x = (width * luma.h // hmax + 7) // 8
        blocks_y = (height * luma.v // vmax + 7) // 8
        grid_w, grid_h = max(1, blocks_x), max(1, blocks_y)
        total_units = grid_w * grid_h
    else:
        grid_w, grid_h = mcus_x * luma.h, mcus_y * luma.v
        total_units = mcus_x * mcus_y
    if grid_w * grid_h > MAX_PIXELS:
        raise DecodeError("jpeg thumbnail too large")

    qdc = (quant.get(luma.tq) or [8])[0] or 8
    plane = bytearray(grid_w * grid_h)
    bits = _Bits(data, entropy_at)
    for c in comps:
        c.pred = 0
    since_restart = 0

    for unit in range(total_units):
        if restart_interval and since_restart == restart_interval:
            if bits.align_and_skip_rst():
                for c in comps:
                    c.pred = 0
                since_restart = 0
        since_restart += 1
        try:
            if single:
                _jpeg_block(bits, luma, dc_tables, ac_tables, progressive)
                bx, by = unit % grid_w, unit // grid_w
                plane[by * grid_w + bx] = _dc_to_grey(luma.pred, qdc, al)
            else:
                mx, my = unit % mcus_x, unit // mcus_x
                for comp in scan:
                    for v in range(comp.v):
                        for h in range(comp.h):
                            _jpeg_block(bits, comp, dc_tables, ac_tables, progressive)
                            if comp is luma:
                                bx, by = mx * luma.h + h, my * luma.v + v
                                plane[by * grid_w + bx] = _dc_to_grey(luma.pred, qdc, al)
        except DecodeError:
            if unit < total_units // 4:
                raise
            break  # a truncated tail still leaves a usable thumbnail
    crop_w = max(1, min(grid_w, (width * luma.h // hmax + 7) // 8))
    crop_h = max(1, min(grid_h, (height * luma.v // vmax + 7) // 8))
    if (crop_w, crop_h) != (grid_w, grid_h):
        plane = bytearray(
            b"".join(bytes(plane[y * grid_w : y * grid_w + crop_w]) for y in range(crop_h))
        )
    return _shrink(Grey(crop_w, crop_h, bytes(plane)), SAMPLE_MAX)


def _jpeg_block(
    bits: _Bits,
    comp: _Component,
    dc_tables: dict[int, list[tuple[int, int]]],
    ac_tables: dict[int, list[tuple[int, int]]],
    progressive: bool,
) -> None:
    dc_table = dc_tables.get(comp.dc_table)
    if dc_table is None:
        raise DecodeError("jpeg: missing DC table")
    size = _decode_huff(bits, dc_table)
    comp.pred += _extend(bits.receive(size), size)
    if progressive:
        return
    ac_table = ac_tables.get(comp.ac_table)
    if ac_table is None:
        raise DecodeError("jpeg: missing AC table")
    k = 1
    while k < 64:
        rs = _decode_huff(bits, ac_table)
        run, size = rs >> 4, rs & 15
        if size == 0:
            if run != 15:
                break
            k += 16
            continue
        k += run + 1
        bits.receive(size)


def _dc_to_grey(pred: int, qdc: int, al: int) -> int:
    value = ((pred << al) * qdc) // 8 + 128
    return 0 if value < 0 else (255 if value > 255 else value)


# --- resample and hash ------------------------------------------------------------------


def _shrink(grey: Grey, limit: int) -> Grey:
    if grey.width <= limit and grey.height <= limit:
        return grey
    return _resample(grey, min(grey.width, limit), min(grey.height, limit))


def _resample(grey: Grey, out_w: int, out_h: int) -> Grey:
    """Box-average down to ``out_w`` x ``out_h``.

    Averaging rather than nearest-neighbour: a re-encode moves individual pixels around,
    and the point of the perceptual hash is to be indifferent to that.
    """
    src = grey.pixels
    w, h = grey.width, grey.height
    out = bytearray(out_w * out_h)
    y_edges = [y * h // out_h for y in range(out_h + 1)]
    x_edges = [x * w // out_w for x in range(out_w + 1)]
    for oy in range(out_h):
        y0, y1 = y_edges[oy], max(y_edges[oy] + 1, y_edges[oy + 1])
        for ox in range(out_w):
            x0, x1 = x_edges[ox], max(x_edges[ox] + 1, x_edges[ox + 1])
            total = 0
            count = 0
            for yy in range(y0, min(y1, h)):
                row = yy * w
                for xx in range(x0, min(x1, w)):
                    total += src[row + xx]
                    count += 1
            out[oy * out_w + ox] = total // count if count else 0
    return Grey(out_w, out_h, bytes(out))


def decode_grey(data: bytes) -> Grey:
    """Bytes to a small greyscale raster. Raises :class:`DecodeError` for what we cannot do."""
    fmt = image_format(data)
    if fmt == "png":
        return _png_grey(data)
    if fmt == "jpeg":
        return _jpeg_grey(data)
    raise DecodeError(f"unsupported:{fmt}")


def dhash(data: bytes, side: int = DHASH_SIDE) -> str:
    """64-bit difference hash as hex. Raises :class:`DecodeError` on an undecodable image.

    dHash rather than aHash: it compares each pixel with its right-hand neighbour, so it
    reads gradients rather than absolute levels and survives the brightness and contrast
    shifts a re-encode or a filter applies. aHash thresholds against the mean and flips
    wholesale when someone darkens the image, which is a one-click evasion.
    """
    grid = _resample(decode_grey(data), side + 1, side)
    px = grid.pixels
    value = 0
    for y in range(side):
        row = y * (side + 1)
        for x in range(side):
            value = (value << 1) | (1 if px[row + x] > px[row + x + 1] else 0)
    return f"{value:0{side * side // 4}x}"


def hamming(a: str, b: str) -> int:
    """Bit distance between two hex hashes. Different widths are treated as maximally far."""
    if len(a) != len(b):
        return len(a) * 4
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def dhash_bands(value: str) -> list[int]:
    """Split a hex dHash into :data:`BAND_COUNT` bands of :data:`BAND_BITS` bits."""
    n = int(value, 16)
    mask = (1 << BAND_BITS) - 1
    return [(n >> (BAND_BITS * i)) & mask for i in range(BAND_COUNT)]


def image_fingerprints(data: bytes) -> tuple[list[Fingerprint], str]:
    """``(fingerprints, image_status)`` for a blob of image bytes.

    The exact hash is always available; the perceptual one is not, and the status string
    is how a WebP that we could not decode stays visible instead of looking like a clean
    "no match".
    """
    out = [Fingerprint(FingerprintKind.IMAGE_SHA256, hashlib.sha256(data).hexdigest())]
    try:
        value = dhash(data)
    except DecodeError as exc:
        return out, str(exc) if str(exc).startswith("unsupported:") else f"undecodable:{exc}"[:60]
    except Exception as exc:  # noqa: BLE001 - a malformed image is data, not a crash
        log.debug("image decode failed: %s", exc)
        return out, f"undecodable:{type(exc).__name__}"
    bits = bin(int(value, 16)).count("1")
    if bits < MIN_DHASH_POPCOUNT or bits > DHASH_SIDE * DHASH_SIDE - MIN_DHASH_POPCOUNT:
        return out, f"low-entropy:{bits}"
    out.append(Fingerprint(FingerprintKind.IMAGE_DHASH, value))
    return out, "ok"


# ---------------------------------------------------------------------------- fetch layer


def _headers() -> dict[str, str]:
    return {"user-agent": USER_AGENT, "accept": "*/*"}


def fetch_coin(
    mint: str,
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    priority: Priority = Priority.DISCOVERY,
    wait_for_slot_s: float = 8.0,
) -> tuple[TokenMeta | None, Receipt]:
    """pump.fun coin metadata. Never raises; a dead provider returns ``(None, UNAVAILABLE)``.

    ``wait_for_slot_s`` is non-zero on purpose: a dossier makes this call alongside every
    other provider, and the non-waiting default silently drops everything after the first
    reservation (see the ``_http`` docstring).
    """
    got = get_json(
        "pumpfun",
        "coins.detail",
        PUMPFUN_COIN_URL.format(mint=mint),
        headers=_headers(),
        ttl_s=COIN_TTL_S,
        stale_grace_s=COIN_TTL_S,
        priority=priority,
        wait_for_slot_s=wait_for_slot_s,
        retries=2,
        conn=conn,
    )
    if not got.ok or not isinstance(got.data, dict):
        return None, got.receipt
    return coin_meta(got.data, chain), got.receipt


def coin_meta(payload: dict[str, Any], chain: Chain = Chain.SOL) -> TokenMeta | None:
    """Map a pump.fun coin payload onto :class:`TokenMeta`. Tolerant of field renames."""
    mint = payload.get("mint") or payload.get("address") or payload.get("ca")
    if not mint:
        return None
    created = payload.get("created_timestamp") or payload.get("createdAt")
    try:
        created_ms = int(created) if created is not None else None
    except (TypeError, ValueError):
        created_ms = None
    if created_ms is not None and created_ms < 10_000_000_000:
        created_ms *= 1000  # some routes answer in seconds
    return TokenMeta(
        mint=str(mint),
        chain=chain,
        name=_as_text(payload.get("name")),
        symbol=_as_text(payload.get("symbol") or payload.get("ticker")),
        description=_as_text(payload.get("description")),
        image_uri=_as_text(payload.get("image_uri") or payload.get("image")),
        metadata_uri=_as_text(payload.get("metadata_uri") or payload.get("uri")),
        created_ms=created_ms,
        creator=_as_text(payload.get("creator") or payload.get("dev")),
    )


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def image_urls(uri: str) -> list[str]:
    """Candidate URLs for an image URI, IPFS gateways first."""
    cid = content_id(uri)
    if cid and cid.startswith("ar:"):
        return [ARWEAVE_GATEWAY.format(cid=cid[3:]), uri]
    if cid:
        urls = [g.format(cid=cid) for g in IPFS_GATEWAYS]
        if uri not in urls:
            urls.append(uri)
        return urls
    return [uri] if uri.startswith(("http://", "https://")) else []


def fetch_image(
    uri: str,
    conn: sqlite3.Connection | None = None,
    *,
    priority: Priority = Priority.RESEARCH,
    wait_for_slot_s: float = 15.0,
    timeout_s: float = 20.0,
) -> tuple[bytes | None, Receipt]:
    """Image bytes, through the limiter and the shared disk cache. Never raises.

    ``_http`` deliberately only speaks JSON, and there is no bytes equivalent to extend
    without editing a file this module does not own. So this borrows every part of it that
    applies — the limiter via ``guarded``, the on-disk cache, ``redact_text``, the
    ``wait_for_slot_s`` semantics and the UNAVAILABLE-not-zero failure contract — and adds
    only the base64 round trip that a binary body needs.
    """
    import base64

    from kaiba.core.events import emit
    from kaiba.core.schemas import EventKind

    urls = image_urls(uri)
    if not urls:
        return None, Receipt(
            provider="ipfs", endpoint="media.image", basis=EvidenceBasis.UNAVAILABLE,
            note="no fetchable url",
        )
    key = f"image:{content_id(uri) or uri}"
    req_digest = digest(key)
    hit = cache_read("ipfs", key, IMAGE_TTL_S)
    if hit is not None and isinstance(hit[0], str):
        try:
            return base64.b64decode(hit[0]), Receipt(
                provider="ipfs", endpoint="media.image", observed_at_ms=hit[2],
                basis=EvidenceBasis.CACHED, request_digest=req_digest,
            )
        except ValueError:
            pass

    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a hard dependency
        return None, Receipt(
            provider="ipfs", endpoint="media.image", basis=EvidenceBasis.UNAVAILABLE,
            note=f"httpx missing: {exc}",
        )

    deadline = time.monotonic() + wait_for_slot_s
    note = "no attempt made"
    for url in urls:
        while True:
            try:
                with guarded("ipfs", "media.image", priority, conn=conn):
                    resp = httpx.get(url, headers=_headers(), timeout=timeout_s, follow_redirects=True)
                    resp.raise_for_status()
                    body = resp.content
            except RateLimited as exc:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(remaining, max(0.05, float(getattr(exc, "retry_after_s", 0.25)))))
                    continue
                note = redact_text(f"rate limited: {exc}")[:200]
                break
            except Exception as exc:  # noqa: BLE001 - a dead gateway is data, not a crash
                note = redact_text(f"{type(exc).__name__}: {exc}")[:200]
                break
            if len(body) > MAX_IMAGE_BYTES:
                note = f"image too large ({len(body)} bytes)"
                break
            if image_format(body) == "unknown":
                note = f"not an image ({body[:8].hex()})"
                break
            cache_write("ipfs", key, base64.b64encode(body).decode("ascii"))
            return body, Receipt(
                provider="ipfs", endpoint="media.image",
                basis=EvidenceBasis.PROVIDER_REPORTED, request_digest=req_digest,
                response_digest=digest(hashlib.sha256(body).hexdigest()),
            )
    try:
        emit(
            EventKind.PROVIDER_ERROR,
            {"provider": "ipfs", "endpoint": "media.image", "detail": note},
            level="warn",
            dedupe_key=f"provider_error:ipfs:media.image:{note[:60]}",
            conn=conn,
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a fetch
        log.debug("could not record image fetch error: %s", exc)
    return None, Receipt(
        provider="ipfs", endpoint="media.image", basis=EvidenceBasis.UNAVAILABLE,
        request_digest=req_digest, note=note[:300],
    )


# ------------------------------------------------------------------------------- registry


def _conn(conn: sqlite3.Connection | None) -> sqlite3.Connection:
    return conn if conn is not None else get_conn()


def coverage(chain: Chain, conn: sqlite3.Connection | None = None) -> tuple[int | None, int]:
    """``(first_scan_ms, mints_seen)`` — how long we have been watching this chain."""
    row = fetch_one(
        _conn(conn),
        "SELECT first_scan_ms, mints_seen FROM dedup_coverage WHERE chain=?",
        (chain.value,),
    )
    if row is None:
        return None, 0
    return int(row["first_scan_ms"]), int(row["mints_seen"])


def _note_coverage(conn: sqlite3.Connection, chain: Chain, at_ms: int, new_mint: bool) -> None:
    """Coverage starts when we *started scanning*, not when the first token we saw was born.

    The conservative choice, deliberately. Indexing ten mints chosen at random says
    nothing about the launches between them, so a backfill cannot be allowed to imply
    completeness by accident — that would turn every unindexed token into a false
    ORIGINAL. A backfill that really did read a contiguous window says so explicitly
    through :func:`declare_coverage`.
    """
    conn.execute(
        "INSERT INTO dedup_coverage(chain, first_scan_ms, last_scan_ms, mints_seen) "
        "VALUES (?,?,?,?) ON CONFLICT(chain) DO UPDATE SET "
        "last_scan_ms=excluded.last_scan_ms, mints_seen=mints_seen+excluded.mints_seen",
        (chain.value, at_ms, at_ms, 1 if new_mint else 0),
    )


def declare_coverage(
    chain: Chain, from_ms: int, conn: sqlite3.Connection | None = None
) -> int | None:
    """Assert that the registry holds *every* launch on ``chain`` since ``from_ms``.

    Only a contiguous backfill may call this — a full page-walk of the launch feed down
    to that timestamp, with no gaps. It is what lets :func:`classify` answer ORIGINAL at
    all, so a false assertion here converts "we have not indexed it" into "nobody used
    this before", which is the one failure mode the whole module is built to avoid.
    Moving coverage later is refused; only an earlier, better-evidenced start is taken.
    """
    c = _conn(conn)
    with tx(c):
        c.execute(
            "INSERT INTO dedup_coverage(chain, first_scan_ms, last_scan_ms, mints_seen) "
            "VALUES (?,?,?,0) ON CONFLICT(chain) DO UPDATE SET "
            "first_scan_ms=MIN(first_scan_ms, excluded.first_scan_ms)",
            (chain.value, from_ms, now_ms()),
        )
    return coverage(chain, c)[0]


def cached_fingerprints(
    chain: Chain, mint: str, conn: sqlite3.Connection | None = None
) -> list[Fingerprint]:
    """Everything we have already computed for a mint. A fingerprint never changes."""
    rows = fetch_all(
        _conn(conn),
        "SELECT kind, value FROM dedup_mint_fingerprints WHERE chain=? AND mint=?",
        (chain.value, mint),
    )
    out: list[Fingerprint] = []
    for row in rows:
        try:
            out.append(Fingerprint(FingerprintKind(str(row["kind"])), str(row["value"])))
        except ValueError:
            continue  # a kind retired in a later version
    return out


def scan_record(chain: Chain, mint: str, conn: sqlite3.Connection | None = None) -> dict | None:
    return fetch_one(
        _conn(conn), "SELECT * FROM dedup_mints WHERE chain=? AND mint=?", (chain.value, mint)
    )


def register(
    chain: Chain,
    mint: str,
    fingerprints: Sequence[Fingerprint],
    created_ms: int | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    image_status: str | None = None,
    dhash_max_distance: int = DEFAULT_DHASH_DISTANCE,
    write: bool = True,
) -> list[Match]:
    """Record a mint's fingerprints and return the ones an older mint already had.

    Out-of-order backfill is the case that has to be right. If this mint is *older* than
    the recorded original, it becomes the original and the incumbent is demoted — the
    alternative is cementing whichever coin our scanner happened to see first, which
    would invert the answer for the exact population (copycats) the signal is for.
    """
    c = _conn(conn)
    at = now_ms()
    matches: list[Match] = []
    ctx = tx(c) if write else _NullTx(c)
    with ctx:
        for fp in fingerprints:
            row = fetch_one(
                c,
                "SELECT first_mint, first_created_ms, first_seen_ms FROM dedup_fingerprints "
                "WHERE chain=? AND kind=? AND value=?",
                (chain.value, fp.kind.value, fp.value),
            )
            if row is not None and str(row["first_mint"]) != mint:
                incumbent_created = row["first_created_ms"]
                incumbent_created = None if incumbent_created is None else int(incumbent_created)
                if _is_older(created_ms, incumbent_created, at, int(row["first_seen_ms"])):
                    # We are the original; the recorded one was a copy all along.
                    matches.append(
                        _match(fp, str(row["first_mint"]), incumbent_created,
                               int(row["first_seen_ms"]), created_ms, flipped=True)
                    )
                    if write:
                        c.execute(
                            "UPDATE dedup_fingerprints SET first_mint=?, first_created_ms=?, "
                            "first_seen_ms=?, hits=hits+1, last_mint=?, last_seen_ms=? "
                            "WHERE chain=? AND kind=? AND value=?",
                            (mint, created_ms, at, mint, at, chain.value, fp.kind.value, fp.value),
                        )
                else:
                    matches.append(
                        _match(fp, str(row["first_mint"]), incumbent_created,
                               int(row["first_seen_ms"]), created_ms)
                    )
                    if write:
                        c.execute(
                            "UPDATE dedup_fingerprints SET hits=hits+1, last_mint=?, last_seen_ms=? "
                            "WHERE chain=? AND kind=? AND value=?",
                            (mint, at, chain.value, fp.kind.value, fp.value),
                        )
            elif row is None and write:
                c.execute(
                    "INSERT OR IGNORE INTO dedup_fingerprints"
                    "(chain, kind, value, first_mint, first_created_ms, first_seen_ms, hits,"
                    " last_mint, last_seen_ms) VALUES (?,?,?,?,?,?,1,?,?)",
                    (chain.value, fp.kind.value, fp.value, mint, created_ms, at, mint, at),
                )
            if fp.kind is FingerprintKind.IMAGE_DHASH:
                matches.extend(
                    _near_image_matches(c, chain, mint, fp.value, created_ms, dhash_max_distance)
                )
                if write:
                    for band_no, band_val in enumerate(dhash_bands(fp.value)):
                        c.execute(
                            "INSERT OR IGNORE INTO dedup_image_bands"
                            "(chain, band_no, band_val, dhash, mint) VALUES (?,?,?,?,?)",
                            (chain.value, band_no, band_val, fp.value, mint),
                        )
            if write:
                c.execute(
                    "INSERT OR REPLACE INTO dedup_mint_fingerprints(chain, mint, kind, value) "
                    "VALUES (?,?,?,?)",
                    (chain.value, mint, fp.kind.value, fp.value),
                )
        if write:
            existed = scan_record(chain, mint, c) is not None
            c.execute(
                "INSERT INTO dedup_mints(chain, mint, created_ms, scanned_ms, image_status) "
                "VALUES (?,?,?,?,?) ON CONFLICT(chain, mint) DO UPDATE SET "
                "scanned_ms=excluded.scanned_ms, created_ms=COALESCE(excluded.created_ms, created_ms), "
                "image_status=COALESCE(excluded.image_status, image_status)",
                (chain.value, mint, created_ms, at, image_status),
            )
            _note_coverage(c, chain, at, not existed)
    # Deduplicate: a near-image match can also be the exact-dhash match.
    seen: set[tuple[str, str]] = set()
    unique: list[Match] = []
    for m in matches:
        key = (m.kind.value, m.original_mint)
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique


class _NullTx:
    """Stands in for ``tx()`` on a dry run, so the write path has one shape."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, *exc: object) -> bool:
        return False


def _is_older(
    created_ms: int | None, other_created_ms: int | None, seen_ms: int, other_seen_ms: int
) -> bool:
    """Is the candidate older than the incumbent? Creation time wins over observation time."""
    if created_ms is not None and other_created_ms is not None:
        return created_ms < other_created_ms
    if created_ms is not None and other_created_ms is None:
        return created_ms < other_seen_ms
    if created_ms is None and other_created_ms is not None:
        return False
    return seen_ms < other_seen_ms


def _match(
    fp: Fingerprint,
    original_mint: str,
    original_created_ms: int | None,
    original_seen_ms: int,
    created_ms: int | None,
    *,
    distance: int | None = None,
    flipped: bool = False,
) -> Match:
    anchor = original_created_ms if original_created_ms is not None else original_seen_ms
    gap = None if created_ms is None else created_ms - anchor
    if flipped and gap is not None:
        gap = -gap
    return Match(
        kind=fp.kind,
        value=fp.value,
        original_mint=original_mint,
        original_created_ms=original_created_ms,
        original_first_seen_ms=original_seen_ms,
        age_gap_ms=gap,
        distance=0 if distance is None else distance,
        copy_of_us=flipped,
    )


def _near_image_matches(
    conn: sqlite3.Connection,
    chain: Chain,
    mint: str,
    value: str,
    created_ms: int | None,
    max_distance: int,
) -> list[Match]:
    """Perceptual near-duplicates, found through the band index rather than a table scan."""
    if max_distance <= 0:
        return []
    if max_distance >= BAND_COUNT:
        log.warning(
            "dhash distance %d >= %d bands: band-index recall is no longer exhaustive",
            max_distance, BAND_COUNT,
        )
    bands = dhash_bands(value)
    clauses = " OR ".join("(band_no=? AND band_val=?)" for _ in bands)
    params: list[object] = [chain.value]
    for band_no, band_val in enumerate(bands):
        params.extend((band_no, band_val))
    rows = fetch_all(
        conn,
        f"SELECT DISTINCT dhash, mint FROM dedup_image_bands WHERE chain=? AND ({clauses})",
        params,
    )
    best: dict[str, tuple[int, str]] = {}
    for row in rows:
        other_mint = str(row["mint"])
        if other_mint == mint:
            continue
        other = str(row["dhash"])
        d = hamming(value, other)
        if d > max_distance:
            continue
        if other_mint not in best or d < best[other_mint][0]:
            best[other_mint] = (d, other)
    out: list[Match] = []
    for other_mint, (d, other) in best.items():
        row = fetch_one(
            conn,
            "SELECT first_mint, first_created_ms, first_seen_ms FROM dedup_fingerprints "
            "WHERE chain=? AND kind=? AND value=?",
            (chain.value, FingerprintKind.IMAGE_DHASH.value, other),
        )
        other_created = None
        other_seen = now_ms()
        if row is not None:
            other_created = None if row["first_created_ms"] is None else int(row["first_created_ms"])
            other_seen = int(row["first_seen_ms"])
        out.append(
            _match(
                Fingerprint(FingerprintKind.IMAGE_DHASH, value),
                other_mint, other_created, other_seen, created_ms, distance=d,
                # We are the older one: they copied us, we did not copy them.
                flipped=_is_older(created_ms, other_created, now_ms(), other_seen),
            )
        )
    return out


# -------------------------------------------------------------------------------- verdict


def fingerprints_for(
    meta: TokenMeta, image: bytes | None = None, extra: Sequence[Fingerprint] = ()
) -> tuple[list[Fingerprint], str | None]:
    """Every fingerprint derivable from what we hold, plus the image status.

    ``extra`` carries fingerprints recovered from the permanent per-mint cache — the
    image hashes of a mint we have already downloaded once. Without it a re-scan would
    quietly stop matching on the image it paid to hash, which is the one fingerprint the
    cache exists to avoid recomputing.
    """
    fps = uri_fingerprints(meta) + text_fingerprints(meta)
    status: str | None = None
    if image is not None:
        image_fps, status = image_fingerprints(image)
        fps = image_fps + fps
    seen = {fp.kind for fp in fps}
    fps.extend(fp for fp in extra if fp.kind not in seen)
    return fps, status


def classify_meta(
    meta: TokenMeta,
    conn: sqlite3.Connection | None = None,
    *,
    image: bytes | None = None,
    image_status: str | None = None,
    register_mint: bool = True,
    dhash_max_distance: int = DEFAULT_DHASH_DISTANCE,
    receipts: Sequence[Receipt] = (),
    unknowns: Sequence[str] = (),
    extra: Sequence[Fingerprint] = (),
) -> Verdict:
    """The verdict for metadata we already hold. :func:`classify` is the fetching wrapper."""
    c = _conn(conn)
    chain = meta.chain
    fps, status = fingerprints_for(meta, image, extra)
    image_status = status or image_status
    coverage_start, _ = coverage(chain, c)

    if not fps:
        return Verdict(
            mint=meta.mint, chain=chain, status=Status.UNKNOWN, basis=EvidenceBasis.UNAVAILABLE,
            created_ms=meta.created_ms, image_status=image_status,
            coverage_start_ms=coverage_start, receipts=tuple(receipts),
            unknowns=(*unknowns, "fingerprints"),
            note="no usable fingerprint: metadata is empty or too generic to identify",
        )

    found = register(
        chain, meta.mint, fps, meta.created_ms, c,
        image_status=image_status, dhash_max_distance=dhash_max_distance, write=register_mint,
    )
    matches = tuple(m for m in found if not m.copy_of_us)
    copied_by = tuple(m for m in found if m.copy_of_us)
    if matches:
        return Verdict(
            mint=meta.mint, chain=chain, status=Status.COPYCAT, basis=EvidenceBasis.DERIVED,
            matches=matches, copied_by=copied_by, fingerprints=tuple(fps),
            created_ms=meta.created_ms, image_status=image_status,
            coverage_start_ms=coverage_start, receipts=tuple(receipts), unknowns=tuple(unknowns),
        )

    # No *older* mint holds any of these fingerprints. That is only evidence of
    # originality if we were already watching when this token was created; otherwise all
    # we know is that our index does not go back far enough, which is UNKNOWN, not
    # ORIGINAL. Newer mints reusing our fingerprints do not change that either way.
    if coverage_start is not None and meta.created_ms is not None and coverage_start <= meta.created_ms:
        return Verdict(
            mint=meta.mint, chain=chain, status=Status.ORIGINAL, basis=EvidenceBasis.DERIVED,
            copied_by=copied_by, fingerprints=tuple(fps), created_ms=meta.created_ms,
            image_status=image_status, coverage_start_ms=coverage_start,
            receipts=tuple(receipts), unknowns=tuple(unknowns),
        )
    reason = (
        "registry has no coverage for this chain"
        if coverage_start is None
        else ("token creation time unknown" if meta.created_ms is None
              else "token predates our registry coverage")
    )
    return Verdict(
        mint=meta.mint, chain=chain, status=Status.UNKNOWN, basis=EvidenceBasis.DERIVED,
        copied_by=copied_by, fingerprints=tuple(fps), created_ms=meta.created_ms,
        image_status=image_status, coverage_start_ms=coverage_start, receipts=tuple(receipts),
        unknowns=(*unknowns, "coverage"),
        note=f"no older mint holds these fingerprints, but {reason}",
    )


def classify(
    mint: str,
    chain: Chain = Chain.SOL,
    conn: sqlite3.Connection | None = None,
    *,
    with_image: bool = False,
    register_mint: bool = True,
    dhash_max_distance: int = DEFAULT_DHASH_DISTANCE,
    meta: TokenMeta | None = None,
) -> Verdict:
    """Original, copycat or unknown, for one mint.

    ``with_image=False`` by default: the CID and text fingerprints cost one small JSON
    call and catch the byte-identical and lightly renamed cases, which is most of them.
    ``with_image=True`` adds the image download and the perceptual hash, which is what
    catches a re-encode. Both are cached permanently per mint, so the cost is paid once.

    The metadata fetch is pump.fun, so it is Solana-only. Everything downstream of the
    metadata is chain-agnostic: pass ``meta`` yourself and this works on any chain,
    including the Pons/Robinhood launches the plan cares about.
    """
    c = _conn(conn)
    mint = safe_normalize(mint, chain)
    receipts: list[Receipt] = []
    unknowns: list[str] = []

    if meta is None and chain is not Chain.SOL:
        return Verdict(
            mint=mint, chain=chain, status=Status.UNKNOWN, basis=EvidenceBasis.UNAVAILABLE,
            coverage_start_ms=coverage(chain, c)[0], unknowns=("metadata",),
            note=f"no metadata source wired for {chain.value}; pass meta= to classify off-chain data",
        )

    if meta is None:
        meta, receipt = fetch_coin(mint, chain, c)
        receipts.append(receipt)
        if meta is None:
            record = scan_record(chain, mint, c)
            cached = cached_fingerprints(chain, mint, c)
            if not cached:
                return Verdict(
                    mint=mint, chain=chain, status=Status.UNKNOWN,
                    basis=EvidenceBasis.UNAVAILABLE, receipts=tuple(receipts),
                    unknowns=("metadata",), coverage_start_ms=coverage(chain, c)[0],
                    note=redact_text(receipt.note or "metadata unavailable")[:200],
                )
            created = None if record is None or record["created_ms"] is None else int(record["created_ms"])
            meta = TokenMeta(mint=mint, chain=chain, created_ms=created)
            unknowns.append("metadata")
            found = register(
                chain, mint, cached, created, c, dhash_max_distance=dhash_max_distance, write=False
            )
            matches = tuple(m for m in found if not m.copy_of_us)
            status = Status.COPYCAT if matches else Status.UNKNOWN
            return Verdict(
                mint=mint, chain=chain, status=status, basis=EvidenceBasis.CACHED,
                matches=matches, copied_by=tuple(m for m in found if m.copy_of_us),
                fingerprints=tuple(cached), created_ms=created,
                coverage_start_ms=coverage(chain, c)[0], receipts=tuple(receipts),
                unknowns=tuple(unknowns),
                note="provider unavailable; answered from the permanent fingerprint cache",
            )

    image: bytes | None = None
    image_status: str | None = None
    extra: list[Fingerprint] = []
    if with_image and meta.image_uri:
        cached = [fp for fp in cached_fingerprints(chain, mint, c)
                  if fp.kind in (FingerprintKind.IMAGE_SHA256, FingerprintKind.IMAGE_DHASH)]
        if any(fp.kind is FingerprintKind.IMAGE_SHA256 for fp in cached):
            extra = cached  # already hashed once, ever; reuse rather than re-download
        else:
            image, receipt = fetch_image(meta.image_uri, c)
            receipts.append(receipt)
            if image is None:
                image_status = "unavailable"
                unknowns.append("image")
    elif with_image:
        image_status = "unavailable"
        unknowns.append("image")

    return classify_meta(
        meta, c, image=image, image_status=image_status, register_mint=register_mint,
        dhash_max_distance=dhash_max_distance, receipts=receipts, unknowns=unknowns, extra=extra,
    )


# ---------------------------------------------------------------------------- bulk + stats


def recent_launches(
    limit: int = 50,
    offset: int = 0,
    conn: sqlite3.Connection | None = None,
    *,
    priority: Priority = Priority.DISCOVERY,
) -> tuple[list[TokenMeta], Receipt]:
    """The newest pump.fun launches, for backfilling the registry and for measurement."""
    got = get_json(
        "pumpfun",
        "coins.list",
        PUMPFUN_LIST_URL,
        params={
            "offset": offset, "limit": limit,
            "sort": "created_timestamp", "order": "DESC", "includeNsfw": "true",
        },
        headers=_headers(),
        ttl_s=60,
        priority=priority,
        wait_for_slot_s=10.0,
        retries=3,
        conn=conn,
    )
    if not got.ok or not isinstance(got.data, list):
        return [], got.receipt
    out = [m for m in (coin_meta(row) for row in got.data if isinstance(row, dict)) if m]
    return out, got.receipt


def classify_batch(
    metas: Iterable[TokenMeta],
    conn: sqlite3.Connection | None = None,
    *,
    dhash_max_distance: int = DEFAULT_DHASH_DISTANCE,
) -> list[Verdict]:
    """Classify a batch in creation order, oldest first.

    Order matters on a backfill: feeding a batch newest-first would register every
    copycat as the original and then demote it one row later. Sorting once here is free
    and removes a whole class of wrong answers.
    """
    c = _conn(conn)
    ordered = sorted(metas, key=lambda m: (m.created_ms is None, m.created_ms or 0))
    return [
        classify_meta(m, c, dhash_max_distance=dhash_max_distance) for m in ordered
    ]


def stats(chain: Chain = Chain.SOL, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Registry size and the observed copycat rate. The sanity check on the whole module."""
    c = _conn(conn)
    first, mints = coverage(chain, c)
    rows = fetch_all(
        c,
        "SELECT kind, COUNT(*) AS n, SUM(hits - 1) AS dupes FROM dedup_fingerprints "
        "WHERE chain=? GROUP BY kind",
        (chain.value,),
    )
    scanned = fetch_one(c, "SELECT COUNT(*) AS n FROM dedup_mints WHERE chain=?", (chain.value,))
    return {
        "chain": chain.value,
        "coverage_start_ms": first,
        "mints_seen": mints,
        "mints_scanned": int(scanned["n"]) if scanned else 0,
        "fingerprints": {str(r["kind"]): int(r["n"]) for r in rows},
        "duplicate_hits": {str(r["kind"]): int(r["dupes"] or 0) for r in rows},
    }


__all__ = [
    "DEFAULT_DHASH_DISTANCE",
    "PROOF_KINDS",
    "DecodeError",
    "Fingerprint",
    "FingerprintKind",
    "Grey",
    "Match",
    "Status",
    "TokenMeta",
    "Verdict",
    "cached_fingerprints",
    "classify",
    "classify_batch",
    "classify_meta",
    "coin_meta",
    "content_id",
    "coverage",
    "declare_coverage",
    "decode_grey",
    "dhash",
    "dhash_bands",
    "fetch_coin",
    "fetch_image",
    "fingerprints_for",
    "hamming",
    "image_format",
    "image_fingerprints",
    "image_urls",
    "normalize_description",
    "normalize_symbol",
    "normalize_text",
    "recent_launches",
    "register",
    "scan_record",
    "stats",
    "text_fingerprints",
    "uri_fingerprints",
]
