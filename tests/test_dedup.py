"""Tests for kaiba.intelligence.dedup.

Offline. The image fixtures under ``tests/fixtures/dedup/`` are real pump.fun launch
images plus ffmpeg-produced variants of one of them, recorded on 2026-09-20; the
perceptual-hash expectations below were cross-checked against ffmpeg's own scaler at the
time they were written, and agreed to within 4 bits of 64.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest

from kaiba.core.schemas import Chain, EvidenceBasis
from kaiba.intelligence import dedup
from kaiba.intelligence.dedup import (
    DEFAULT_DHASH_DISTANCE,
    DecodeError,
    Fingerprint,
    FingerprintKind,
    Status,
    TokenMeta,
)

FIXTURES = Path(__file__).parent / "fixtures" / "dedup"


# ------------------------------------------------------------------ synthetic PNG helper


def _png(width: int, height: int, rows: list[list[int]], *, colour: int = 0, depth: int = 8,
         filters: list[int] | None = None, palette: bytes = b"") -> bytes:
    """Encode a PNG we control completely, so decoder tests do not need a real file.

    ``rows`` holds raw sample bytes per scanline (already in the layout the colour type
    implies). ``filters`` chooses the filter byte per row, which is how the four
    non-trivial unfilter branches get exercised.
    """
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
    stride = (width * channels * depth + 7) // 8
    bpp = max(1, channels * depth // 8)
    raw = bytearray()
    prev = bytes(stride)
    for y, row in enumerate(rows):
        line = bytes(row)
        assert len(line) == stride, f"row {y}: {len(line)} != {stride}"
        ftype = (filters or [0] * height)[y]
        if ftype == 0:
            enc = line
        elif ftype == 1:
            enc = bytes((line[i] - (line[i - bpp] if i >= bpp else 0)) & 0xFF for i in range(stride))
        elif ftype == 2:
            enc = bytes((line[i] - prev[i]) & 0xFF for i in range(stride))
        elif ftype == 3:
            enc = bytes(
                (line[i] - (((line[i - bpp] if i >= bpp else 0) + prev[i]) >> 1)) & 0xFF
                for i in range(stride)
            )
        else:
            enc = bytes(
                (line[i] - dedup._paeth(
                    line[i - bpp] if i >= bpp else 0, prev[i], prev[i - bpp] if i >= bpp else 0
                )) & 0xFF
                for i in range(stride)
            )
        raw += bytes([ftype]) + enc
        prev = line
    body = b"\x89PNG\r\n\x1a\x0a" + chunk(b"IHDR", ihdr)
    if palette:
        body += chunk(b"PLTE", palette)
    return body + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def _gradient_rows(width: int, height: int) -> list[list[int]]:
    return [[(x * 255) // max(1, width - 1) for x in range(width)] for _ in range(height)]


# ------------------------------------------------------------------ text normalisation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Doge Killer", "doge killer"),
        ("  DOGE   killer  ", "doge killer"),
        ("🚀🚀 Doge Killer 🚀", "doge killer"),
        ("Doge​Killer", "dogekiller"),
        ("ＤＯＧＥ Killer", "doge killer"),
        ("Dоgе Killer", "doge killer"),  # Cyrillic о and е
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_text_folds_decoration(raw, expected):
    assert dedup.normalize_text(raw) == expected


def test_normalize_text_keeps_genuinely_different_names_apart():
    assert dedup.normalize_text("Doge Killer") != dedup.normalize_text("Doge Slayer")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("$DOGE", "doge"), ("doge", "doge"), ("D.O.G.E", "doge"), ("ＤＯＧＥ", "doge"), ("🐕", "")],
)
def test_normalize_symbol_strips_ticker_decoration(raw, expected):
    assert dedup.normalize_symbol(raw) == expected


def test_text_fingerprints_skip_degenerate_values():
    meta = TokenMeta(mint="M", chain=Chain.SOL, name="ab", symbol="x", description="short")
    assert dedup.text_fingerprints(meta) == []


def test_text_fingerprints_skip_description_that_is_just_the_name():
    meta = TokenMeta(
        mint="M", chain=Chain.SOL, name="the doge killer coin",
        symbol="dogek", description="The Doge Killer Coin",
    )
    kinds = {fp.kind for fp in dedup.text_fingerprints(meta)}
    assert kinds == {FingerprintKind.NAME, FingerprintKind.SYMBOL}


def test_text_fingerprints_hash_long_descriptions():
    meta = TokenMeta(mint="M", chain=Chain.SOL, description="a" * 400)
    (fp,) = dedup.text_fingerprints(meta)
    assert fp.kind is FingerprintKind.DESCRIPTION
    assert len(fp.value) == 32


@pytest.mark.parametrize(
    "watermark",
    [
        "Launched on discord.gg/uxento",
        "Created on https://rapidlaunch.io",
        "Deployed using https://j7tracker.io",
        "https://pump.fun",
        "www.example.com/token",
    ],
)
def test_deployer_watermarks_produce_no_description_fingerprint(watermark):
    """The three real ones accounted for 80% of all repeated descriptions in a live sample."""
    meta = TokenMeta(mint="M", chain=Chain.SOL, description=watermark)
    assert dedup.text_fingerprints(meta) == []


def test_a_real_description_survives_url_stripping():
    meta = TokenMeta(
        mint="M", chain=Chain.SOL,
        description="A meme coin built around aura and main-character energy. https://x.com/foo",
    )
    (fp,) = dedup.text_fingerprints(meta)
    assert fp.kind is FingerprintKind.DESCRIPTION


def test_the_same_description_with_and_without_a_link_is_one_fingerprint():
    a = TokenMeta(mint="A", chain=Chain.SOL, description="the first true aura coin of the season")
    b = TokenMeta(mint="B", chain=Chain.SOL,
                  description="The first true aura coin of the season https://t.me/aura")
    assert dedup.text_fingerprints(a) == dedup.text_fingerprints(b)


# ------------------------------------------------------------------------- content ids


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("https://ipfs.io/ipfs/QmeLDjGpxxyqj5n8F8KacPUKKVbvisA3BBMqaexV4n65X3",
         "QmeLDjGpxxyqj5n8F8KacPUKKVbvisA3BBMqaexV4n65X3"),
        ("https://pump.mypinata.cloud/ipfs/bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i",
         "bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i"),
        ("ipfs://bafybeien3rrnz5xafcz7gsehn7mrvxifovipiwpmpolz6tjhsu7iv7ock4",
         "bafybeien3rrnz5xafcz7gsehn7mrvxifovipiwpmpolz6tjhsu7iv7ock4"),
        ("https://cdn.example.com/logo.png", None),
        ("", None),
        (None, None),
    ],
)
def test_content_id_extracts_only_content_addressed_ids(uri, expected):
    assert dedup.content_id(uri) == expected


def test_same_cid_on_two_gateways_is_one_fingerprint():
    cid = "bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i"
    assert dedup.content_id(f"https://ipfs.io/ipfs/{cid}") == dedup.content_id(
        f"https://pump.mypinata.cloud/ipfs/{cid}"
    )


def test_image_urls_prefer_a_gateway_that_answers():
    urls = dedup.image_urls("https://ipfs.io/ipfs/bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i")
    assert urls[0].startswith("https://pump.mypinata.cloud/ipfs/")


def test_image_urls_are_empty_for_a_non_url():
    assert dedup.image_urls("not a url") == []


# --------------------------------------------------------------------------- PNG decode


@pytest.mark.parametrize("ftype", [0, 1, 2, 3, 4])
def test_png_decodes_under_every_filter_type(ftype):
    rows = _gradient_rows(16, 16)
    data = _png(16, 16, rows, filters=[ftype] * 16)
    grey = dedup.decode_grey(data)
    assert (grey.width, grey.height) == (16, 16)
    assert grey.pixels[0] == 0
    assert grey.pixels[15] == 255


def test_png_filters_all_agree_on_the_same_image():
    rows = _gradient_rows(16, 16)
    hashes = {dedup.dhash(_png(16, 16, rows, filters=[f] * 16)) for f in range(5)}
    assert len(hashes) == 1


def test_png_rgb_and_greyscale_of_the_same_image_hash_identically():
    grey_rows = _gradient_rows(16, 16)
    rgb_rows = [[v for x in row for v in (x, x, x)] for row in grey_rows]
    assert dedup.dhash(_png(16, 16, grey_rows)) == dedup.dhash(_png(16, 16, rgb_rows, colour=2))


def test_png_palette_is_resolved_through_plte():
    palette = bytes([0, 0, 0, 255, 255, 255])
    rows = [[0, 1] * 8 for _ in range(16)]
    grey = dedup.decode_grey(_png(16, 16, rows, colour=3, palette=palette))
    assert grey.pixels[0] == 0
    assert grey.pixels[1] > 240


def test_png_four_bit_depth_is_scaled_to_full_range():
    # Two 4-bit samples per byte: 0x0F is a black pixel next to a white one.
    rows = [[0x0F] * 8 for _ in range(16)]
    grey = dedup.decode_grey(_png(16, 16, rows, depth=4))
    assert grey.pixels[0] == 0
    assert grey.pixels[1] == 255


def test_png_sixteen_bit_depth_uses_the_high_byte():
    rows = [[b for x in range(16) for b in (x * 17, 0)] for _ in range(16)]
    grey = dedup.decode_grey(_png(16, 16, rows, depth=16))
    assert grey.pixels[0] == 0
    assert grey.pixels[15] == 255


def test_png_interlaced_is_refused_rather_than_guessed():
    data = bytearray(_png(8, 8, _gradient_rows(8, 8)))
    data[8 + 8 + 12] = 1  # IHDR interlace byte
    with pytest.raises(DecodeError, match="interlaced"):
        dedup.decode_grey(bytes(data))


def test_png_oversized_is_refused():
    # Forge an IHDR claiming 8000x8000 without carrying the pixels.
    data = bytearray(_png(8, 8, _gradient_rows(8, 8)))
    data[16:24] = struct.pack(">II", 8000, 8000)
    data[8 + 8 + 13 : 8 + 8 + 17] = struct.pack(">I", zlib.crc32(data[12:29]) & 0xFFFFFFFF)
    with pytest.raises(DecodeError, match="too large"):
        dedup.decode_grey(bytes(data))


def test_garbage_is_a_decode_error_not_a_crash():
    with pytest.raises(DecodeError):
        dedup.decode_grey(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)


# ------------------------------------------------------------------------------- dHash


def test_dhash_is_deterministic():
    data = _png(16, 16, _gradient_rows(16, 16))
    assert dedup.dhash(data) == dedup.dhash(data)


def test_dhash_is_scale_invariant():
    small = _png(16, 16, _gradient_rows(16, 16))
    large = _png(64, 64, _gradient_rows(64, 64))
    assert dedup.hamming(dedup.dhash(small), dedup.dhash(large)) <= 2


def test_dhash_separates_a_different_image():
    left = _png(16, 16, _gradient_rows(16, 16))
    right = _png(16, 16, [[255 - v for v in row] for row in _gradient_rows(16, 16)])
    assert dedup.hamming(dedup.dhash(left), dedup.dhash(right)) > 8


def test_hamming_treats_different_widths_as_maximally_far():
    assert dedup.hamming("ffff", "ffffffffffffffff") == 16


def test_dhash_bands_reassemble_the_hash():
    value = "0123456789abcdef"
    bands = dedup.dhash_bands(value)
    assert len(bands) == dedup.BAND_COUNT
    rebuilt = sum(b << (dedup.BAND_BITS * i) for i, b in enumerate(bands))
    assert f"{rebuilt:016x}" == value


def test_band_count_guarantees_recall_at_the_default_threshold():
    # Pigeonhole: distance <= BAND_COUNT - 1 means at least one band matches exactly.
    assert DEFAULT_DHASH_DISTANCE <= dedup.BAND_COUNT - 1


# ----------------------------------------------------------------- real image fixtures


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_real_png_and_jpeg_both_decode():
    assert dedup.image_format(_fixture("original.png")) == "png"
    assert dedup.image_format(_fixture("photo.jpg")) == "jpeg"
    assert dedup.decode_grey(_fixture("original.png")).width > 1
    assert dedup.decode_grey(_fixture("photo.jpg")).width > 1


def test_jpeg_dc_decoder_produces_a_plausible_thumbnail():
    grey = dedup.decode_grey(_fixture("photo.jpg"))
    values = set(grey.pixels)
    assert len(values) > 16, "a DC thumbnail that is nearly flat means the huffman walk desynced"
    assert 10 < sum(grey.pixels) / len(grey.pixels) < 245


def test_perceptual_hash_survives_a_jpeg_reencode():
    """The whole reason a perceptual hash exists: re-encoding is the common evasion."""
    original = dedup.dhash(_fixture("original.png"))
    reencoded = dedup.dhash(_fixture("reencoded.jpg"))
    assert original != reencoded or True  # equality is fine, closeness is the requirement
    assert dedup.hamming(original, reencoded) <= DEFAULT_DHASH_DISTANCE


def test_perceptual_hash_survives_a_brightness_shift():
    assert dedup.hamming(
        dedup.dhash(_fixture("original.png")), dedup.dhash(_fixture("brightened.jpg"))
    ) <= DEFAULT_DHASH_DISTANCE


def test_exact_hash_does_not_survive_a_reencode_which_is_why_both_exist():
    exact_a, _ = dedup.image_fingerprints(_fixture("original.png"))
    exact_b, _ = dedup.image_fingerprints(_fixture("reencoded.jpg"))
    assert exact_a[0].value != exact_b[0].value


def test_two_unrelated_real_images_are_far_apart():
    assert dedup.hamming(
        dedup.dhash(_fixture("original.png")), dedup.dhash(_fixture("photo.jpg"))
    ) > DEFAULT_DHASH_DISTANCE


def test_webp_is_reported_unsupported_and_still_gets_an_exact_hash():
    fps, status = dedup.image_fingerprints(_fixture("unsupported.webp"))
    assert status == "unsupported:webp"
    assert [fp.kind for fp in fps] == [FingerprintKind.IMAGE_SHA256]


def test_low_entropy_images_are_not_given_a_perceptual_fingerprint():
    flat = _png(16, 16, [[200] * 16 for _ in range(16)])
    fps, status = dedup.image_fingerprints(flat)
    assert status.startswith("low-entropy")
    assert [fp.kind for fp in fps] == [FingerprintKind.IMAGE_SHA256]


# ------------------------------------------------------------------------ coin payloads


def _coins() -> list[dict]:
    return json.loads((FIXTURES / "coins.json").read_text(encoding="utf-8"))


def test_coin_meta_maps_a_real_payload():
    meta = dedup.coin_meta(_coins()[0])
    assert meta is not None
    assert meta.mint and meta.symbol and meta.image_uri
    assert meta.created_ms and meta.created_ms > 1_700_000_000_000


def test_coin_meta_promotes_seconds_to_milliseconds():
    meta = dedup.coin_meta({"mint": "M", "created_timestamp": 1789884626})
    assert meta is not None and meta.created_ms == 1789884626000


def test_coin_meta_without_a_mint_is_none():
    assert dedup.coin_meta({"name": "no mint here"}) is None


def test_a_real_recorded_factory_cluster_is_caught_by_the_cid_alone(tmp_db):
    """Three consecutive real launches: one creator, one image, three different names.

    Recorded from the live API on 2026-09-20. They are part of a 20-mint cluster sharing
    one image CID under one creator wallet, with twelve different tickers between them.
    Every text fingerprint misses this; the content hash catches all of it. It is the
    single clearest argument for ranking the CID above the name.
    """
    payloads = json.loads((FIXTURES / "copycat_cluster.json").read_text(encoding="utf-8"))
    metas = [dedup.coin_meta(p) for p in payloads]
    assert all(m is not None for m in metas)
    names = {dedup.normalize_text(m.name) for m in metas}
    assert len(names) == len(metas), "the fixture must have genuinely different names"

    verdicts = dedup.classify_batch(metas, tmp_db)
    copies = [v for v in verdicts if v.is_copycat]
    assert len(copies) == len(metas) - 1
    for v in copies:
        assert v.proof_match is not None
        assert v.proof_match.kind is FingerprintKind.IMAGE_CID
        assert v.proof_match.original_mint == verdicts[0].mint


# ---------------------------------------------------------------------------- registry


def _meta(mint: str, created_ms: int, **kw) -> TokenMeta:
    base = {"name": "Doge Killer", "symbol": "DOGEK", "description": "the one true doge killer coin"}
    base.update(kw)
    return TokenMeta(mint=mint, chain=Chain.SOL, created_ms=created_ms, **base)


def test_first_mint_owns_the_fingerprint_and_the_second_is_a_copycat(tmp_db):
    first = dedup.classify_meta(_meta("AAA", 1_000_000), tmp_db)
    second = dedup.classify_meta(_meta("BBB", 2_000_000), tmp_db)
    assert first.status in (Status.ORIGINAL, Status.UNKNOWN)
    assert second.status is Status.COPYCAT
    assert second.best is not None
    assert second.best.original_mint == "AAA"
    assert second.best.age_gap_ms == 1_000_000


def test_out_of_order_backfill_promotes_the_older_mint(tmp_db):
    """Originality is a timestamp question: whoever we saw first is not the answer."""
    late_seen_but_newer = dedup.classify_meta(_meta("NEW", 2_000_000), tmp_db)
    assert late_seen_but_newer.status in (Status.ORIGINAL, Status.UNKNOWN)
    older = dedup.classify_meta(_meta("OLD", 1_000_000), tmp_db)
    assert older.status is not Status.COPYCAT, "the seed of a cluster is not a copy of its output"
    assert older.copied_by, "it must still be told that a newer mint reused its fingerprints"
    assert older.copied_by[0].original_mint == "NEW"
    third = dedup.classify_meta(_meta("THIRD", 3_000_000), tmp_db)
    assert third.best is not None
    assert third.best.original_mint == "OLD", "the registry must now point at the older mint"


def test_reclassifying_the_same_mint_does_not_make_it_a_copy_of_itself(tmp_db):
    dedup.classify_meta(_meta("AAA", 1_000_000), tmp_db)
    again = dedup.classify_meta(_meta("AAA", 1_000_000), tmp_db)
    assert again.status is not Status.COPYCAT


def test_a_shared_cid_is_the_strongest_match(tmp_db):
    cid = "https://ipfs.io/ipfs/bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i"
    dedup.classify_meta(_meta("AAA", 1_000, image_uri=cid), tmp_db)
    verdict = dedup.classify_meta(
        _meta("BBB", 2_000, name="Totally Different", symbol="DIFF",
              description="nothing alike at all here", image_uri=cid),
        tmp_db,
    )
    assert verdict.status is Status.COPYCAT
    assert verdict.best is not None and verdict.best.kind is FingerprintKind.IMAGE_CID


def test_best_match_prefers_the_strongest_kind(tmp_db):
    cid = "https://ipfs.io/ipfs/bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i"
    dedup.classify_meta(_meta("AAA", 1_000, image_uri=cid), tmp_db)
    verdict = dedup.classify_meta(_meta("BBB", 2_000, image_uri=cid), tmp_db)
    assert verdict.best is not None and verdict.best.kind is FingerprintKind.IMAGE_CID
    kinds = {m.kind for m in verdict.matches}
    assert FingerprintKind.NAME in kinds and FingerprintKind.SYMBOL in kinds


def test_proof_match_separates_a_content_hash_from_a_word_match(tmp_db):
    """A shared CID is arithmetic; a shared ticker is an opinion. Sizing needs both apart."""
    cid = "https://ipfs.io/ipfs/bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i"
    dedup.classify_meta(_meta("AAA", 1_000, image_uri=cid), tmp_db)
    hard = dedup.classify_meta(_meta("BBB", 2_000, image_uri=cid), tmp_db)
    assert hard.proof_match is not None and hard.proof_match.proof

    dedup.classify_meta(
        TokenMeta(mint="CCC", chain=Chain.SOL, created_ms=1_000, name="Only A Shared Name"), tmp_db
    )
    soft = dedup.classify_meta(
        TokenMeta(mint="DDD", chain=Chain.SOL, created_ms=2_000, name="only a shared name"), tmp_db
    )
    assert soft.status is Status.COPYCAT
    assert soft.proof_match is None, "a name match must never be presented as proof"


def test_a_symbol_collision_alone_is_still_reported_as_the_weakest_kind(tmp_db):
    dedup.classify_meta(
        TokenMeta(mint="AAA", chain=Chain.SOL, created_ms=1_000, symbol="DOGE"), tmp_db
    )
    verdict = dedup.classify_meta(
        TokenMeta(mint="BBB", chain=Chain.SOL, created_ms=2_000, symbol="$doge"), tmp_db
    )
    assert verdict.status is Status.COPYCAT
    assert verdict.best is not None and verdict.best.kind is FingerprintKind.SYMBOL


def test_unrelated_tokens_do_not_match(tmp_db):
    dedup.classify_meta(_meta("AAA", 1_000), tmp_db)
    other = dedup.classify_meta(
        TokenMeta(mint="BBB", chain=Chain.SOL, created_ms=2_000, name="Unrelated Thing",
                  symbol="UNREL", description="a completely different description entirely"),
        tmp_db,
    )
    assert other.status is not Status.COPYCAT


# ------------------------------------------------- near-duplicate image band index


def test_near_duplicate_image_is_found_through_the_band_index(tmp_db):
    original = dedup.dhash(_fixture("original.png"))
    reencoded = dedup.dhash(_fixture("reencoded.jpg"))
    dedup.register(
        Chain.SOL, "AAA", [Fingerprint(FingerprintKind.IMAGE_DHASH, original)], 1_000, tmp_db
    )
    matches = dedup.register(
        Chain.SOL, "BBB", [Fingerprint(FingerprintKind.IMAGE_DHASH, reencoded)], 2_000, tmp_db
    )
    assert matches, "a re-encode within the threshold must be found"
    assert matches[0].original_mint == "AAA"
    assert matches[0].distance is not None and matches[0].distance <= DEFAULT_DHASH_DISTANCE


def test_a_distant_image_is_not_a_near_duplicate(tmp_db):
    dedup.register(
        Chain.SOL, "AAA", [Fingerprint(FingerprintKind.IMAGE_DHASH, "0000000000000000")], 1_000, tmp_db
    )
    matches = dedup.register(
        Chain.SOL, "BBB", [Fingerprint(FingerprintKind.IMAGE_DHASH, "ffffffffffffffff")], 2_000, tmp_db
    )
    assert matches == []


def test_the_older_image_is_not_reported_as_a_copy_of_the_newer(tmp_db):
    dedup.register(
        Chain.SOL, "NEW", [Fingerprint(FingerprintKind.IMAGE_DHASH, "0f0f0f0f0f0f0f0f")], 5_000, tmp_db
    )
    matches = dedup.register(
        Chain.SOL, "OLD", [Fingerprint(FingerprintKind.IMAGE_DHASH, "0f0f0f0f0f0f0f0e")], 1_000, tmp_db
    )
    assert matches, "the collision is still reported"
    assert all(m.copy_of_us for m in matches), "but with us on the original side of it"


# ------------------------------------------------------ unknown is a first-class answer


def test_no_coverage_means_unknown_not_original(tmp_db):
    """A cold registry must not manufacture confidence out of an empty cache."""
    verdict = dedup.classify_meta(_meta("AAA", 1_000_000), tmp_db)
    assert verdict.status is Status.UNKNOWN
    assert "coverage" in verdict.unknowns
    assert verdict.basis is EvidenceBasis.DERIVED


def test_scanning_a_mint_does_not_claim_coverage_back_to_its_birth(tmp_db):
    """Indexing one old mint says nothing about the launches around it."""
    dedup.classify_meta(_meta("SEED", 1_000), tmp_db)
    start, _ = dedup.coverage(Chain.SOL, tmp_db)
    assert start is not None and start > 1_000, "coverage must start when we did, not when it did"


def test_declare_coverage_is_what_lets_a_backfill_answer_original(tmp_db):
    verdict = dedup.classify_meta(_meta("AAA", 1_000_000), tmp_db)
    assert verdict.status is Status.UNKNOWN
    dedup.declare_coverage(Chain.SOL, 500_000, tmp_db)
    later = dedup.classify_meta(
        TokenMeta(mint="BBB", chain=Chain.SOL, created_ms=1_500_000, name="Unrelated Name Here",
                  symbol="UNH", description="a description shared with nothing at all"),
        tmp_db,
    )
    assert later.status is Status.ORIGINAL


def test_declare_coverage_never_moves_the_start_later(tmp_db):
    dedup.declare_coverage(Chain.SOL, 1_000, tmp_db)
    assert dedup.declare_coverage(Chain.SOL, 9_000, tmp_db) == 1_000


def test_original_requires_coverage_that_predates_the_token(tmp_db):
    dedup.declare_coverage(Chain.SOL, 1_000, tmp_db)
    dedup.classify_meta(_meta("SEED", 1_000), tmp_db)
    verdict = dedup.classify_meta(
        TokenMeta(mint="LATER", chain=Chain.SOL, created_ms=9_000, name="Something Else Here",
                  symbol="SEH", description="an entirely unrelated description of a coin"),
        tmp_db,
    )
    assert verdict.status is Status.ORIGINAL
    assert verdict.coverage_start_ms is not None and verdict.coverage_start_ms <= 9_000


def test_a_token_older_than_our_coverage_is_unknown(tmp_db):
    dedup.declare_coverage(Chain.SOL, 5_000, tmp_db)
    dedup.classify_meta(_meta("SEED", 5_000), tmp_db)
    verdict = dedup.classify_meta(
        TokenMeta(mint="ANCIENT", chain=Chain.SOL, created_ms=1_000, name="Ancient Coin Here",
                  symbol="ANC", description="predates everything our registry has indexed"),
        tmp_db,
    )
    assert verdict.status is Status.UNKNOWN
    assert verdict.note and "predates" in verdict.note


def test_unknown_creation_time_cannot_be_called_original(tmp_db):
    dedup.classify_meta(_meta("SEED", 1_000), tmp_db)
    verdict = dedup.classify_meta(
        TokenMeta(mint="NOTIME", chain=Chain.SOL, name="No Timestamp Coin",
                  symbol="NTC", description="a coin whose creation time we never learned"),
        tmp_db,
    )
    assert verdict.status is Status.UNKNOWN


def test_empty_metadata_is_unknown_and_unavailable(tmp_db):
    verdict = dedup.classify_meta(TokenMeta(mint="EMPTY", chain=Chain.SOL), tmp_db)
    assert verdict.status is Status.UNKNOWN
    assert verdict.basis is EvidenceBasis.UNAVAILABLE
    assert "fingerprints" in verdict.unknowns


def test_a_copycat_is_still_a_copycat_before_coverage_is_established(tmp_db):
    """Coverage gates ORIGINAL, never COPYCAT: a positive match needs no coverage."""
    dedup.classify_meta(_meta("AAA", 1_000_000), tmp_db)
    verdict = dedup.classify_meta(_meta("BBB", 1_000_001), tmp_db)
    assert verdict.status is Status.COPYCAT


# ------------------------------------------------------------------- fetch + classify


def test_classify_returns_unknown_when_the_provider_is_down(tmp_db, monkeypatch):
    from kaiba.core.schemas import Receipt

    monkeypatch.setattr(
        dedup, "fetch_coin",
        lambda *a, **k: (None, Receipt(provider="pumpfun", endpoint="coins.detail",
                                       basis=EvidenceBasis.UNAVAILABLE, note="ConnectError")),
    )
    verdict = dedup.classify("SOMEMINT", Chain.SOL, tmp_db)
    assert verdict.status is Status.UNKNOWN
    assert verdict.basis is EvidenceBasis.UNAVAILABLE
    assert "metadata" in verdict.unknowns


def test_classify_falls_back_to_the_permanent_fingerprint_cache(tmp_db, monkeypatch):
    from kaiba.core.schemas import Receipt

    dedup.classify_meta(_meta("AAA", 1_000), tmp_db)
    dedup.classify_meta(_meta("BBB", 2_000), tmp_db)
    monkeypatch.setattr(
        dedup, "fetch_coin",
        lambda *a, **k: (None, Receipt(provider="pumpfun", endpoint="coins.detail",
                                       basis=EvidenceBasis.UNAVAILABLE, note="down")),
    )
    verdict = dedup.classify("BBB", Chain.SOL, tmp_db)
    assert verdict.status is Status.COPYCAT
    assert verdict.basis is EvidenceBasis.CACHED
    assert verdict.best is not None and verdict.best.original_mint == "AAA"


def test_classify_on_a_non_solana_chain_says_so_instead_of_querying_pumpfun(tmp_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("pump.fun must not be asked about a Robinhood-chain mint")

    monkeypatch.setattr(dedup, "fetch_coin", boom)
    verdict = dedup.classify("0x" + "ab" * 20, Chain.BSC, tmp_db)
    assert verdict.status is Status.UNKNOWN
    assert verdict.basis is EvidenceBasis.UNAVAILABLE
    assert verdict.note and "pass meta=" in verdict.note


def test_classify_works_on_another_chain_when_metadata_is_supplied(tmp_db):
    a = TokenMeta(mint="0xaaa", chain=Chain.BSC, created_ms=1_000, name="Shared Name Here")
    b = TokenMeta(mint="0xbbb", chain=Chain.BSC, created_ms=2_000, name="shared name here")
    dedup.classify_meta(a, tmp_db)
    assert dedup.classify_meta(b, tmp_db).status is Status.COPYCAT


def test_classify_uses_supplied_metadata_without_fetching(tmp_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("classify must not fetch when metadata is supplied")

    monkeypatch.setattr(dedup, "fetch_coin", boom)
    verdict = dedup.classify("AAA", Chain.SOL, tmp_db, meta=_meta("AAA", 1_000))
    assert verdict.mint == "AAA"


def test_classify_with_image_records_an_unavailable_image(tmp_db, monkeypatch):
    from kaiba.core.schemas import Receipt

    monkeypatch.setattr(
        dedup, "fetch_image",
        lambda *a, **k: (None, Receipt(provider="ipfs", endpoint="media.image",
                                       basis=EvidenceBasis.UNAVAILABLE, note="403")),
    )
    meta = _meta("AAA", 1_000, image_uri="https://ipfs.io/ipfs/bafkreihlk472usxlgcwzoh4blzibysj4hmv7k7jn7xbjfgf3g72lwdhq4i")
    verdict = dedup.classify("AAA", Chain.SOL, tmp_db, with_image=True, meta=meta)
    assert verdict.image_status == "unavailable"
    assert "image" in verdict.unknowns
    # The CID fingerprint still works without the bytes, which is the point of using it.
    assert any(fp.kind is FingerprintKind.IMAGE_CID for fp in verdict.fingerprints)


def test_classify_with_image_hashes_real_bytes(tmp_db, monkeypatch):
    from kaiba.core.schemas import Receipt

    payload = _fixture("original.png")
    monkeypatch.setattr(
        dedup, "fetch_image",
        lambda *a, **k: (payload, Receipt(provider="ipfs", endpoint="media.image")),
    )
    meta = _meta("AAA", 1_000, image_uri="https://cdn.example.com/a.png")
    verdict = dedup.classify("AAA", Chain.SOL, tmp_db, with_image=True, meta=meta)
    assert verdict.image_status == "ok"
    kinds = {fp.kind for fp in verdict.fingerprints}
    assert {FingerprintKind.IMAGE_SHA256, FingerprintKind.IMAGE_DHASH} <= kinds


def test_a_rescan_reuses_the_cached_image_hash_instead_of_downloading_again(tmp_db):
    """The cache exists so an image is hashed once ever; it must still be *matched* on."""
    from kaiba.core.schemas import Receipt

    calls: list[str] = []

    def once(uri, *a, **k):
        calls.append(uri)
        return _fixture("original.png"), Receipt(provider="ipfs", endpoint="media.image")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(dedup, "fetch_image", once)
    try:
        uri = "https://cdn.example.com/a.png"
        dedup.classify("AAA", Chain.SOL, tmp_db, with_image=True, meta=_meta("AAA", 1_000, image_uri=uri))
        again = dedup.classify(
            "AAA", Chain.SOL, tmp_db, with_image=True, meta=_meta("AAA", 1_000, image_uri=uri)
        )
    finally:
        monkey.undo()
    assert len(calls) == 1, "the image must be downloaded once, ever"
    kinds = {fp.kind for fp in again.fingerprints}
    assert FingerprintKind.IMAGE_SHA256 in kinds, "the cached hash must still take part in matching"
    assert FingerprintKind.IMAGE_DHASH in kinds


def test_fetch_image_refuses_a_uri_it_cannot_fetch(tmp_db):
    body, receipt = dedup.fetch_image("not-a-url", tmp_db)
    assert body is None
    assert receipt.basis is EvidenceBasis.UNAVAILABLE


# ------------------------------------------------------------------ batch and caching


def test_classify_batch_registers_oldest_first_whatever_order_it_is_given(tmp_db):
    metas = [_meta("C", 3_000), _meta("A", 1_000), _meta("B", 2_000)]
    verdicts = {v.mint: v for v in dedup.classify_batch(metas, tmp_db)}
    assert verdicts["A"].status is not Status.COPYCAT
    assert verdicts["B"].best is not None and verdicts["B"].best.original_mint == "A"
    assert verdicts["C"].best is not None and verdicts["C"].best.original_mint == "A"


def test_fingerprints_are_cached_permanently_per_mint(tmp_db):
    dedup.classify_meta(_meta("AAA", 1_000), tmp_db)
    cached = dedup.cached_fingerprints(Chain.SOL, "AAA", tmp_db)
    assert {fp.kind for fp in cached} == {
        FingerprintKind.NAME, FingerprintKind.SYMBOL, FingerprintKind.DESCRIPTION
    }


def test_stats_reports_the_registry_and_the_duplicate_count(tmp_db):
    dedup.classify_meta(_meta("AAA", 1_000), tmp_db)
    dedup.classify_meta(_meta("BBB", 2_000), tmp_db)
    out = dedup.stats(Chain.SOL, tmp_db)
    assert out["mints_scanned"] == 2
    assert out["fingerprints"]["name"] == 1
    assert out["duplicate_hits"]["name"] == 1


def test_register_dry_run_does_not_write(tmp_db):
    dedup.register(Chain.SOL, "AAA", [Fingerprint(FingerprintKind.NAME, "x y z")], 1_000, tmp_db,
                   write=False)
    assert dedup.cached_fingerprints(Chain.SOL, "AAA", tmp_db) == []


def test_verdict_summary_is_human_readable(tmp_db):
    dedup.classify_meta(_meta("AAA", 1_000), tmp_db)
    verdict = dedup.classify_meta(_meta("BBB", 3_600_001), tmp_db)
    assert "copycat via" in verdict.summary()


# ---------------------------------------------------------------------- live smoke test


@pytest.mark.live
def test_live_pumpfun_endpoint_still_answers(tmp_db):
    metas, receipt = dedup.recent_launches(limit=5, conn=tmp_db)
    assert receipt.basis is not EvidenceBasis.UNAVAILABLE
    assert metas and all(m.mint for m in metas)
