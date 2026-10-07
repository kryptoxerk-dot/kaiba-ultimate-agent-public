# Codex integration 2026-10-07, owner reiterated after the specific permission request:
# selectable J7/filter source, full financial admission, atomic launch reservations,
# publication freshness and failed-status precedence. Claude retains the architecture.
# Codex P8-TL-PONS-SIZING 2026-10-07: Pons protocol fee is deducted from input;
# correct only that sizing branch, retaining route/risk caps and zero-creator-tax model.
"""Tweet launcher: a watched account posts -> we launch a token on it and dev-buy ~5%.

Owner request, 2026-10-06: "launch automatically every time certain people tweet; choose
the tweet that is the context; attach the uploaded image (or a generated logo); auto-buy 5%
with one wallet", on Solana, BNB and Robinhood, from the agent wallet.

Pipeline (one process, the ``tweet-launch`` service):

  selected J7 / X feed   ->  plan()  ->  record (tweet_launches)  ->  [live] send()
                                                                  ->  observe() copycats

* **Choosing the post** (:func:`score_post`): a launchable post is short, has an image, and
  names one thing. Replies and reposts are skipped unless configured. Every verdict, launch
  or skip, is recorded with its reasons -- the skip rows are half of the measurement.
* **Name and ticker** (:func:`derive_identity`): deterministic, in order of strength -- a
  cashtag the author wrote, a quoted phrase, a hashtag, a one/two-word post, an ALL-CAPS
  word, then the most salient capitalised word. Majors ($BTC, $SOL ...) are never used.
* **Image**: the post's own media. Posts without media are recorded and, while
  ``require_image`` is on, not launched (no logo generator is wired; it needs an image-model
  key). The author's profile picture is never used: that is their likeness, and pump.fun's
  terms (2026-09-25) bar implying endorsement.
* **Dev buy** (:func:`dev_buy_native`): sized as a share of supply where a curve model
  exists -- pump.fun's classic curve (5% ~= 1.485 SOL incl. 1.25% fee) and Pons V2 (5% ~=
  0.0893 ETH incl. 1% fee). Refuse amounts above ``max_buy_native`` rather than undersizing.
  Launchpads without a model
  (flap, fourmeme, bonk, bags, trench) need an explicit ``buy_amt_native``.
* **Why the downside is bounded**: the dev buy is the FIRST buy on a fresh curve. Whatever
  others do afterwards, they can only sell what they bought, so the curve can never price
  our bag below our own entry; with no buyers at all we sell back for what we paid less
  ~2x fee. That is a structural property of a bonding curve, not of the market -- it does
  NOT hold on Pons, whose anti-sniper tax is 99% at second 0 (``lanes.py``), which is why
  robinhood ships ``live: false`` until a dev buy is proven exempt.
* **Exits**: never here. A live launch writes a SUBMITTED ``orders`` row with the created
  token; ``executor.reconcile_all`` books the fill from GMGN's report, opens the position and
  arms protection; the watchdog's ladder does every sale (owner rule 2026-10-06).
* **Arming**: live needs this file's config ``mode: live`` + ``armed_by``, the chain's
  ``live: true``, the ``tweet-launch`` Lane in ``kaiba/core/schemas.py`` AND a live
  ``tweet-launch`` lane in ``config/risk.yaml`` (``executor._check_mode`` -- the kill switch,
  reduce-only and paused-entries brakes all apply).

Nothing here reads the database for longer than one short query (AGENTS.md DB read rule).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

from kaiba.core.config import get_risk, get_settings
from kaiba.core.db import connect, fetch_all, fetch_one, jdump, jload, tx
from kaiba.core.schemas import Chain, digest
from kaiba.ingest.x_stream import XPost

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "tweet_launch.yaml"
LANE_VALUE = "tweet-launch"

# --------------------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainRoute:
    chain: Chain
    dex: str
    max_buy_native: Decimal
    buy_amt_native: Decimal | None = None
    live: bool = False
    #: Extra gmgn-cli flags that route the token's fees to its holders (owner 2026-10-06:
    #: "100% fees goes to holder"). e.g. ["--is-cashback"] on pump, a dividend
    #: --flap-rate-conf on Flap. Empty = the launchpad default (creator keeps the fee).
    holder_fee_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    mode: str = "shadow"
    armed_by: str = ""
    dev_buy_supply_pct: float = 5.0
    chains: dict[Chain, ChainRoute] = field(default_factory=dict)
    #: More launchpads on the same chain, launched IN ADDITION to ``chains[chain]`` (owner
    #: 2026-10-07: "bnb four.meme + flap.sh combo"). Launch id tl:<tweet>:<chain>:<dex>.
    extra_routes: dict[Chain, tuple[ChainRoute, ...]] = field(default_factory=dict)
    daily_launch_cap: int = 5
    daily_native_cap: dict[Chain, Decimal] = field(default_factory=dict)
    per_author_cooldown_s: int = 900
    max_tweet_age_s: int = 20
    slippage_pct: int = 30
    min_score: float = 2.0
    post_kinds: tuple[str, ...] = ("post", "quote")
    require_image: bool = True
    link_tweet_as_twitter: bool = True
    observe_horizons_s: tuple[int, ...] = (120, 600, 1800)
    accounts: dict[str, tuple[Chain, ...]] = field(default_factory=dict)  # lower-case handle -> chains
    #: tweet_refs (early alpha / vamp) watches these too; they never trigger a tweet launch.
    alpha: Any = None
    account_kinds: dict[str, tuple[str, ...]] = field(default_factory=dict)
    namer_enabled: bool = True
    namer_model: str = "claude-opus-5-5"
    namer_timeout_s: float = 8.0
    #: Who picks name/ticker/launch, tried in order. Owner 2026-10-07: "use hermes for the picker".
    namer_backends: tuple[str, ...] = ("hermes", "anthropic")
    hermes_profile: str = "kaiba-operator"
    logo_generate: bool = True
    logo_timeout_s: float = 15.0
    activity_poll_s: float = 3.0
    #: Solana create flags (speed). A create+dev-buy cannot be sandwiched -- the token does not
    #: exist before our transaction -- so anti-MEV bundling only adds delay there; a priority
    #: fee buys faster inclusion instead.
    sol_anti_mev: bool = False
    sol_priority_fee: Decimal | None = Decimal("0.0005")
    #: Vamp a watched post's launch that already has volume (owner 2026-10-07: "Vamp launches
    #: with volume / Just use same everything"). ``vamp_launches`` live|shadow|off.
    vamp_launches: str = "shadow"
    vamp_window_s: int = 60
    vamp_min_volume_usd: Decimal = Decimal(0)

    @property
    def live(self) -> bool:
        return self.mode == "live" and bool(self.armed_by.strip())


def _dec(v: Any) -> Decimal | None:
    return None if v is None or v == "" else Decimal(str(v))


def load_config(path: Path | None = None) -> Config:
    raw = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8")) or {}
    chains, extra = {}, {}

    def _route(ch: Chain, c: dict[str, Any]) -> ChainRoute:
        return ChainRoute(
            chain=ch, dex=str(c["dex"]), max_buy_native=_dec(c.get("max_buy_native")) or Decimal(0),
            buy_amt_native=_dec(c.get("buy_amt_native")), live=bool(c.get("live", False)),
            holder_fee_args=_holder_fee_args(ch, str(c["dex"]), c.get("holder_fees")),
        )

    for name, c in (raw.get("chains") or {}).items():
        ch = Chain(name)
        chains[ch] = _route(ch, c)
        if c.get("also"):
            extra[ch] = tuple(_route(ch, x) for x in c["also"])
    accounts, kinds = {}, {}
    for a in raw.get("accounts") or []:
        h = str(a["handle"]).lstrip("@").lower()
        accounts[h] = tuple(Chain(x) for x in a.get("chains") or ["sol"])
        if a.get("post_kinds"):
            kinds[h] = tuple(a["post_kinds"])
    return Config(
        mode=str(raw.get("mode", "shadow")), armed_by=str(raw.get("armed_by") or ""),
        dev_buy_supply_pct=float(raw.get("dev_buy_supply_pct", 5.0)), chains=chains, extra_routes=extra,
        daily_launch_cap=int(raw.get("daily_launch_cap", 5)),
        daily_native_cap={Chain(k): Decimal(str(v)) for k, v in (raw.get("daily_native_cap") or {}).items()},
        per_author_cooldown_s=int(raw.get("per_author_cooldown_s", 900)),
        max_tweet_age_s=int(raw.get("max_tweet_age_s", 20)),
        slippage_pct=int(raw.get("slippage_pct", 30)),
        min_score=float(raw.get("min_score", 2.0)),
        post_kinds=tuple(raw.get("post_kinds") or ("post", "quote")),
        require_image=bool(raw.get("require_image", True)),
        link_tweet_as_twitter=bool(raw.get("link_tweet_as_twitter", True)),
        observe_horizons_s=tuple(int(x) for x in raw.get("observe_horizons_s") or (120, 600, 1800)),
        accounts=accounts, account_kinds=kinds,
        alpha=_alpha_config(raw),
        namer_enabled=bool((raw.get("namer") or {}).get("enabled", True)),
        namer_model=str((raw.get("namer") or {}).get("model") or "claude-opus-5-5"),
        namer_timeout_s=float((raw.get("namer") or {}).get("timeout_s", 8.0)),
        namer_backends=tuple((raw.get("namer") or {}).get("backends") or ("hermes", "anthropic")),
        hermes_profile=str((raw.get("namer") or {}).get("hermes_profile") or "kaiba-operator"),
        logo_generate=bool((raw.get("logo") or {}).get("generate", True)),
        logo_timeout_s=float((raw.get("logo") or {}).get("timeout_s", 15.0)),
        activity_poll_s=float(raw.get("activity_poll_s", 3.0)),
        sol_anti_mev=bool((raw.get("speed") or {}).get("sol_anti_mev", False)),
        sol_priority_fee=_dec((raw.get("speed") or {}).get("sol_priority_fee", "0.0005")),
        vamp_launches=str((raw.get("vamp") or {}).get("tweet_launches", "shadow")),
        vamp_window_s=int((raw.get("vamp") or {}).get("window_s", 60)),
        vamp_min_volume_usd=Decimal(str((raw.get("vamp") or {}).get("min_volume_usd", 0))),
    )


def _alpha_config(raw: dict[str, Any]) -> Any:
    from kaiba.execution.tweet_refs import parse_alpha_config
    return parse_alpha_config(raw)


def route_for(cfg: Config, chain: Chain, dex: str) -> ChainRoute | None:
    """The configured route for (chain, launchpad): the primary or one of its ``also`` routes."""
    for r in (cfg.chains.get(chain), *cfg.extra_routes.get(chain, ())):
        if r is not None and r.dex == dex:
            return r
    return None


def watched_handles(cfg: Config) -> set[str]:
    """Every author the shared socket must let through: launch accounts plus alpha accounts."""
    return set(cfg.accounts) | (set(cfg.alpha.accounts) if cfg.alpha is not None else set())


def _holder_fee_args(chain: Chain, dex: str, spec: Any) -> tuple[str, ...]:
    """gmgn-cli flags for "100% of fees to holders" on this launchpad.

    * pump: ``--is-cashback`` (creator fee -> traders). UNVERIFIED after 2026-09-12, when
      pump.fun deprecated Cashback for new launches in favour of "Holder Rewards"; gmgn-cli
      1.6.6 has no Holder Rewards flag. A refusal is a failed launch, never a launch with
      the fee silently going to us.
    * flap: a 1% buy/sell tax with ``dividend_bps: 10000`` -- all of it paid to holders.
    * anything else: no holder-fee mechanism known here -> config error (raised at load).
    """
    if not spec:
        return ()
    if spec == "cashback" and dex == "pump":
        return ("--is-cashback",)
    if isinstance(spec, dict) and spec.get("dividend_tax_bps") is not None and dex == "flap":
        bps = int(spec["dividend_tax_bps"])
        conf = {"buy_tax_rate": bps, "sell_tax_rate": bps, "mkt_bps": 0, "deflation_bps": 0,
                "dividend_bps": 10000, "lp_bps": 0, "minimum_share_balance": 10000,
                "recipient_type": "split", "twitter_account": "", "split_conf": []}
        return ("--flap-rate-conf", json.dumps(conf, separators=(",", ":")))
    if isinstance(spec, dict) and spec.get("dividend_fee_pct") is not None and dex == "fourmeme":
        # four.meme fee plan: fee_rate is a WHOLE percent; the shares route it. All to dividends.
        # UNVERIFIED against a live create: a refusal fails only this route, nothing is sent.
        conf = {"fee_plan": True, "recipient_address": "", "fee_rate": int(spec["dividend_fee_pct"]),
                "burn_rate": 0, "divide_rate": 100, "liquidity_rate": 0, "recipient_rate": 0}
        return ("--fourmeme-rate-conf", json.dumps(conf, separators=(",", ":")))
    raise ValueError(f"holder_fees {spec!r} not supported on {chain.value}/{dex}")


# --------------------------------------------------------------------------------------
# choosing the post, naming the token
# --------------------------------------------------------------------------------------

#: Never launch a ticker that IS an established asset: buyers searching it find the real one.
MAJORS = frozenset({
    "BTC", "ETH", "SOL", "BNB", "USDT", "USDC", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
    "SHIB", "DOT", "LINK", "MATIC", "POL", "LTC", "BCH", "PEPE", "WIF", "BONK", "TRUMP", "HOOD",
    "WBTC", "WETH", "WSOL", "USD1", "DAI", "SUI", "APT", "ARB", "OP", "XLM", "NEAR", "UNI",
})
STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have he her his how
i if in into is it its just like me more most my no not now of on one or our out over she so
some than that the their them then there these they this to too up us very was we were what
when where which who why will with would you your yes all any new get got going go great big
good today tomorrow tonight time day week year people thing things really much many make made
said say says see seen know think thank thanks love want need let lets let's ok okay rt via
amp https http www com im i'm it's don't dont can't cant we're they're you're wow lol
update updates icymi breaking just alert news report reports watch live thread official latest
announced announces says said today's week's daily weekly via read more here now video photo
""".split())

_URL = re.compile(r"https?://\S+")
#: A brand-shaped word: an internal capital after a lower-case letter (SpaceX, OpenAI, xAI, iPhone).
_BRAND = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])[A-Za-z][A-Za-z0-9]*[a-z][A-Z][A-Za-z0-9]*$|^[a-z]+[A-Z][A-Za-z0-9]*$")
#: Media outlets, institutions and acronyms: context words, never the subject of a coin.
NOT_A_SUBJECT = frozenset("""
FOX CNN CNBC BBC NBC ABC CBS MSNBC NYT WSJ AP AFP REUTERS BLOOMBERG POLITICO AXIOS TMZ NPR PBS
CEO CFO CTO COO USA US UK EU UN NATO AI IPO ETF SEC FBI CIA DOJ GOP DNC NFL NBA MLB NHL UFC TV PM AM
GDP CPI FED FOMC IRS DOGE NASA LIVE OFFICIAL EXCLUSIVE BREAKING UPDATE ICYMI JUST NEWS ALERT
""".split())
#: "<Subject> is / launches / says ..." -- the first word is what the post is about.
SUBJECT_VERBS = frozenset("""is are was were has have had will just announces announced launches launched
says said unveils unveiled reveals revealed hits hit buys bought sells sold drops dropped surges soars
plans wants gets got becomes became""".split())
_MENTION = re.compile(r"(?<!\w)@\w{1,15}")
_CASHTAG = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9]{1,9})\b")
_HASHTAG = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_]{1,24})\b")
_QUOTED = re.compile(r"[\"“”']([^\"“”']{2,32})[\"“”']")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'’]*")


@dataclass(frozen=True)
class Identity:
    name: str
    symbol: str
    basis: str            # cashtag | quoted | hashtag | short_post | caps_word | salient_word


def _clean(text: str) -> str:
    t = _URL.sub(" ", text or "")
    t = re.sub(r"^(\s*@\w+)+", " ", t)          # leading reply mentions
    return re.sub(r"\s+", " ", t).strip()


def _symbolize(phrase: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]", "", phrase).upper()
    return s[:10]


def _titled(phrase: str) -> str:
    words = phrase.strip().split()
    return " ".join(w if w.isupper() and len(w) > 1 else w[:1].upper() + w[1:] for w in words)[:32]


def _ok_symbol(sym: str) -> bool:
    return 2 <= len(sym) <= 10 and sym not in MAJORS and not sym.isdigit()


def derive_identity(text: str) -> Identity | None:
    """Name and ticker from the post. ``None`` when nothing in it is nameable."""
    t = _clean(text)
    if not t:
        return None
    for m in _CASHTAG.finditer(t):
        sym = m.group(1).upper()
        if _ok_symbol(sym):
            return Identity(name=_titled(m.group(1)), symbol=sym, basis="cashtag")
    for m in _QUOTED.finditer(t):
        phrase = m.group(1).strip()
        if 1 <= len(phrase.split()) <= 3 and _ok_symbol(_symbolize(phrase)):
            return Identity(name=_titled(phrase), symbol=_symbolize(phrase), basis="quoted")
    for m in _HASHTAG.finditer(t):
        tag = m.group(1)
        if tag.lower() not in STOPWORDS and _ok_symbol(_symbolize(tag)):
            return Identity(name=_titled(tag), symbol=_symbolize(tag), basis="hashtag")
    words = [w for w in _WORD.findall(_MENTION.sub(" ", t))]
    content = [w for w in words if w.lower().strip("'’") not in STOPWORDS]
    if 1 <= len(words) <= 2 and content:
        phrase = " ".join(words)
        if _ok_symbol(_symbolize(phrase)):
            return Identity(name=_titled(phrase), symbol=_symbolize(phrase), basis="short_post")
    # 2026-10-07, owner: "$FOX ... out of context". The post was "SpaceX is still undervalued,
    # says Kyle Reidhead on FOX Business" -- FOX is the channel, SpaceX the subject. A brand
    # (internal capital: SpaceX, OpenAI, xAI, iPhone) or the sentence subject ("Tesla is ...")
    # outranks a stray ALL-CAPS word, and outlets / acronyms are never the ticker.
    for w in content:
        core = w.strip("'’")
        if _BRAND.match(core) and _ok_symbol(_symbolize(core)) and core.upper() not in NOT_A_SUBJECT:
            return Identity(name=core[:32], symbol=_symbolize(core), basis="brand_word")
    if len(words) >= 2:
        first, nxt = words[0].strip("'’"), words[1].lower()
        if (first[:1].isupper() and first.lower() not in STOPWORDS and nxt in SUBJECT_VERBS
                and first.upper() not in NOT_A_SUBJECT and _ok_symbol(_symbolize(first))):
            return Identity(name=_titled(first), symbol=_symbolize(first), basis="subject")
    for w in content:
        if w.isupper() and 3 <= len(w) <= 10 and _ok_symbol(w) and w not in NOT_A_SUBJECT:
            return Identity(name=_titled(w), symbol=w, basis="caps_word")
    caps = [w for w in content[1:] if w[:1].isupper() and len(w) >= 3 and w.upper() not in NOT_A_SUBJECT] or \
           [w for w in content if len(w) >= 4 and w.upper() not in NOT_A_SUBJECT]
    if caps:
        w = max(caps, key=len)
        sym = _symbolize(w)
        if _ok_symbol(sym):
            return Identity(name=_titled(w), symbol=sym, basis="salient_word")
    return None


def score_post(post: XPost, cfg: Config) -> tuple[float, list[str]]:
    """Launch-worthiness. Positive reasons add, negative subtract; reasons are recorded."""
    reasons: list[str] = []
    score = 0.0
    kinds = cfg.account_kinds.get(post.author, cfg.post_kinds)
    if post.kind not in kinds:
        return -99.0, [f"kind:{post.kind}_not_in_{'/'.join(kinds)}"]
    text = _clean(post.text)
    if post.kind == "reply" and not (_CASHTAG.search(text) or _QUOTED.search(text)):
        # A KOL reply moves volume when it NAMES a token; "gm" / "lol" must not spend a launch.
        return -99.0, ["reply_without_token_name"]
    n_words = len(_WORD.findall(text))
    if post.media:
        score += 1.5
        reasons.append("has_media")
    if n_words == 0 and not post.media:
        return -99.0, ["empty"]
    if n_words <= 3:
        score += 1.5
        reasons.append(f"very_short:{n_words}w")
    elif n_words <= 12:
        score += 0.75
        reasons.append(f"short:{n_words}w")
    elif n_words > 40:
        score -= 1.0
        reasons.append(f"long:{n_words}w")
    if _CASHTAG.search(text) or _QUOTED.search(text) or _HASHTAG.search(text):
        score += 1.0
        reasons.append("explicit_name")
    if _URL.search(post.text or "") and n_words <= 6:
        score -= 0.5
        reasons.append("link_post")
    return score, reasons


# --------------------------------------------------------------------------------------
# dev buy sizing
# --------------------------------------------------------------------------------------

# pump.fun classic curve (kaiba/execution/scanner.py:167-175): virtual 1.073e9 tokens x 30 SOL,
# 1e9 total supply, buy fee 125 bps on top (curve_price.py:146-148). Creators can start from
# other reserves; a GMGN create uses the default.
PUMP_VIRTUAL_TOKENS = Decimal(1_073_000_000)
PUMP_VIRTUAL_SOL = Decimal(30)
PUMP_SUPPLY = Decimal(1_000_000_000)
PUMP_FEE = Decimal("0.0125")
# Pons V2 (docs/research/line4-antisniper-tax-pons.md:122-125): phantom quote 1.68 ETH against
# the full 1e9 launch supply; fee 100 bps (lanes.py:2359).
PONS_PHANTOM_ETH = Decimal("1.68")
PONS_FEE = Decimal("0.01")
# Flap, BNB-quoted (evm_price.FlapCurve: (x + h)(y + r) = K). MEASURED 2026-10-06 from the
# portal record of 12 Flap launches in the previous 20 min: 10 shared r = 6.14 BNB,
# h = 107,036,752 tokens, supply 1e9, graduation at 8e8 sold (the other 2 were a
# stablecoin-quoted curve, r = 767.4). Fee 100 bps (evm_price.py:144).
FLAP_VIRTUAL_BNB = Decimal("6.14")
FLAP_H_TOKENS = Decimal(107_036_752)
FLAP_SUPPLY = Decimal(1_000_000_000)
FLAP_FEE = Decimal("0.01")
# four.meme, BNB-quoted TokenManager2 curve (evm_price.FourMemeCurve). MEASURED 2026-10-07 from
# 3/3 BNB-quoted launches (the rest were USDT/other-quoted): virtual quote at launch 6.164384 BNB,
# virtual tokens 1,073,972,603, fee 100 bps on top, graduation at 18 BNB raised.
FOURMEME_VIRTUAL_BNB = Decimal("6.164384")
FOURMEME_VIRTUAL_TOKENS = Decimal(1_073_972_603)
FOURMEME_SUPPLY = Decimal(1_000_000_000)
FOURMEME_FEE = Decimal("0.01")


def dev_buy_native(route: ChainRoute, supply_pct: float) -> tuple[Decimal | None, float | None, str]:
    """(native amount, modelled share, basis); refuse over-cap targets, round native UP."""
    p = Decimal(str(supply_pct)) / 100
    amt: Decimal | None
    share: float | None = supply_pct
    if route.buy_amt_native is not None:
        amt, share, basis = route.buy_amt_native, None, "configured_amount"
    elif route.chain is Chain.SOL and route.dex == "pump":
        want = PUMP_SUPPLY * p
        amt = PUMP_VIRTUAL_SOL * PUMP_VIRTUAL_TOKENS / (PUMP_VIRTUAL_TOKENS - want) - PUMP_VIRTUAL_SOL
        amt *= 1 + PUMP_FEE
        basis = "pump_classic_curve"
    elif route.chain is Chain.ROBINHOOD and route.dex in {"pons", "pons_v2"}:
        if supply_pct == 5.0:
            from kaiba.execution.tweet_launch_policy import quote_five_percent_fee_on_input

            amt = quote_five_percent_fee_on_input(total_supply_atoms=10**27,
                virtual_token_atoms=10**27, virtual_native_atoms=int(PONS_PHANTOM_ETH * 10**18),
                fee_bps=int(PONS_FEE * 10000), native_decimals=18).native_amount
        else:
            amt = PONS_PHANTOM_ETH * (1 / (1 - p) - 1) / (1 - PONS_FEE)
        basis = "pons_v2_phantom_quote"
    elif route.chain is Chain.BSC and route.dex == "fourmeme":
        x0, t0 = FOURMEME_VIRTUAL_BNB, FOURMEME_VIRTUAL_TOKENS
        amt = x0 * t0 / (t0 - FOURMEME_SUPPLY * p) - x0
        amt *= 1 + FOURMEME_FEE
        basis = "fourmeme_bnb_curve"
    elif route.chain is Chain.BSC and route.dex == "flap":
        x0 = FLAP_SUPPLY + FLAP_H_TOKENS
        amt = FLAP_VIRTUAL_BNB * x0 / (x0 - FLAP_SUPPLY * p) - FLAP_VIRTUAL_BNB
        amt *= 1 + FLAP_FEE
        basis = "flap_bnb_curve"
    else:
        return None, None, f"no_curve_model:{route.chain.value}/{route.dex}"
    if amt > route.max_buy_native:
        return None, None, "five_percent_exceeds_route_cap"
    from kaiba.core.schemas import NATIVE_DECIMALS
    q = Decimal(1).scaleb(-NATIVE_DECIMALS[route.chain])
    amt = amt.quantize(q, rounding=ROUND_UP)
    if amt > route.max_buy_native:
        return None, None, "five_percent_exceeds_route_cap"
    return amt, share, basis


# --------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------


@dataclass
class Plan:
    launch_id: str
    tweet_id: str
    author: str
    chain: Chain
    dex: str
    mode: str
    verdict: str
    reasons: list[str]
    score: float
    name: str | None = None
    symbol: str | None = None
    image_source: str = "none"
    image_url: str | None = None
    image_b64: str | None = None          # a generated logo; never written to the database
    buy_amt_native: Decimal | None = None
    supply_pct: float | None = None
    buy_basis: str | None = None
    argv: list[str] | None = None
    decided_ms: int = 0
    decide_latency_ms: int | None = None


def _description(post: XPost) -> str:
    # The source, plainly. Never "official", never in the author's voice.
    body = _clean(post.text)[:300]
    return f"Inspired by a post on X: “{body}”" if body else "Inspired by a post on X."


def build_argv(plan: Plan, post: XPost, cfg: Config, wallet: str) -> list[str]:
    assert plan.name and plan.symbol and plan.buy_amt_native is not None
    argv = [
        "cooking", "create",
        "--chain", plan.chain.value, "--dex", plan.dex, "--from", wallet,
        "--name", plan.name, "--symbol", plan.symbol,
        "--buy-amt", format(plan.buy_amt_native, "f"),
        "--slippage", str(cfg.slippage_pct),
        "--description", _description(post),
    ]
    if plan.image_b64:
        argv += ["--image", plan.image_b64]
    elif plan.image_url:
        argv += ["--image-url", plan.image_url]
    if cfg.link_tweet_as_twitter:
        argv += ["--twitter", post.url]
    route = route_for(cfg, plan.chain, plan.dex)
    fee_args = list(route.holder_fee_args) if route else []
    if "--fourmeme-rate-conf" in fee_args:
        i = fee_args.index("--fourmeme-rate-conf") + 1
        conf = json.loads(fee_args[i])
        conf["recipient_address"] = wallet
        fee_args[i] = json.dumps(conf, separators=(",", ":"))
    if "--flap-rate-conf" in fee_args:
        # recipient_type "split" requires a split list; mkt_bps is 0, so nothing reaches it.
        i = fee_args.index("--flap-rate-conf") + 1
        conf = json.loads(fee_args[i])
        conf["split_conf"] = [{"recipient": wallet, "bps": 10000}]
        fee_args[i] = json.dumps(conf, separators=(",", ":"))
    argv += fee_args
    if plan.chain is Chain.SOL:
        if cfg.sol_anti_mev:
            argv += ["--anti-mev"]
        if cfg.sol_priority_fee:
            argv += ["--priority-fee", format(cfg.sol_priority_fee, "f")]
    return argv + ["--yes"]


def recordable_argv(argv: list[str] | None) -> list[str] | None:
    """The argv with a base64 logo replaced by its size (the row stays small)."""
    if argv is None:
        return None
    out = list(argv)
    if "--image" in out:
        i = out.index("--image") + 1
        out[i] = f"<base64 {len(out[i])} chars>"
    return out


def plan_post(post: XPost, cfg: Config, *, now_ms: int | None = None,
              wallets: dict[Chain, str] | None = None, ai: Any = None, logo: Any = None) -> list[Plan]:
    """One plan per configured chain for the author. Pure: no I/O.

    ``ai`` (``tweet_creative.AiIdentity``) is the model's read of the post: when present it
    chooses the name/ticker AND whether the post is launchable, replacing the word picker
    and the score threshold (the post-kind filter and every money check still apply).
    ``logo`` (``tweet_creative.Logo``) supplies a generated image when the post has none.
    """
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    chains = cfg.accounts.get(post.author)
    if not chains:
        return []
    score, reasons = score_post(post, cfg)
    if ai is not None:
        ident = Identity(name=ai.name, symbol=ai.symbol, basis=f"ai:{ai.model}")
        reasons = [*reasons, f"ai:{'launch' if ai.launch else 'skip'}:{ai.reason}"[:160], f"ai_ms:{ai.latency_ms}"]
    else:
        ident = derive_identity(post.text)
    out = []
    pairs = []
    for ch in chains:
        pairs.append((ch, cfg.chains.get(ch), f"tl:{post.tweet_id}:{ch.value}"))
        pairs += [(ch, r, f"tl:{post.tweet_id}:{ch.value}:{r.dex}") for r in cfg.extra_routes.get(ch, ())]
    for ch, route, launch_id in pairs:
        p = Plan(
            launch_id=launch_id, tweet_id=post.tweet_id, author=post.author,
            chain=ch, dex=route.dex if route else "?", mode="live" if (cfg.live and route and route.live) else "shadow",
            verdict="skip", reasons=list(reasons), score=score, decided_ms=now,
            decide_latency_ms=None if post.created_ms is None else now - post.created_ms,
        )
        if route is None:
            p.reasons.append(f"no_route:{ch.value}")
            out.append(p)
            continue
        if ident is not None:
            p.name, p.symbol = ident.name, ident.symbol
            p.reasons.append(f"name_basis:{ident.basis}")
        if logo is not None and logo.source != "none":
            p.image_source, p.image_url, p.image_b64 = logo.source, logo.url, logo.b64
            if logo.note:
                p.reasons.append(f"logo:{logo.note}"[:160])
        elif post.media and logo is None:
            # no logo decision was made (pure planning / tests): the post's image as-is
            p.image_source, p.image_url = "tweet_media", post.media[0]
        elif logo is not None and logo.note:
            p.reasons.append(f"logo:{logo.note}"[:160])
        amt, share, basis = dev_buy_native(route, cfg.dev_buy_supply_pct)
        p.buy_amt_native, p.supply_pct, p.buy_basis = amt, share, basis
        blockers = []
        if score <= -99:
            blockers.append("post_kind_or_empty")
        elif ai is not None:
            if not ai.launch:
                blockers.append("ai_says_skip")
        elif score < cfg.min_score:
            blockers.append(f"score_below:{score:.2f}<{cfg.min_score}")
        if ident is None:
            blockers.append("no_nameable_word")
        if cfg.require_image and p.image_source == "none":
            blockers.append("no_image")
        if amt is None:
            blockers.append(basis)
        age = None if post.created_ms is None else now - post.created_ms
        if age is None or age < 0:
            blockers.append("invalid_publication_time")
        elif age > cfg.max_tweet_age_s * 1000:
            blockers.append(f"too_late:{age}ms")
        p.reasons += blockers
        if not blockers:
            p.verdict = "launch"
            wallet = (wallets or {}).get(ch) or "<chain-wallet>"
            p.argv = build_argv(p, post, cfg, wallet)
        out.append(p)
    return out


# --------------------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------------------


def record_post(conn: sqlite3.Connection, post: XPost) -> bool:
    with tx(conn):
        cur = conn.execute(
            "INSERT OR IGNORE INTO tweet_launch_tweets (tweet_id, author, kind, text, media_json, "
            "created_ms, received_ms, feed_delay_ms, backend, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (post.tweet_id, post.author, post.kind, post.text, jdump(list(post.media)),
             post.created_ms, post.received_ms, post.feed_delay_ms, post.backend, jdump(dict(post.raw))),
        )
    return cur.rowcount == 1


def record_plan(conn: sqlite3.Connection, p: Plan) -> None:
    with tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO tweet_launches (launch_id, tweet_id, author, chain, dex, mode, verdict, "
            "reasons_json, score, name, symbol, image_source, image_url, buy_amt_native, supply_pct, "
            "buy_basis, argv_json, decided_ms, decide_latency_ms, state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.launch_id, p.tweet_id, p.author, p.chain.value, p.dex, p.mode, p.verdict,
             jdump(p.reasons), p.score, p.name, p.symbol, p.image_source, p.image_url,
             None if p.buy_amt_native is None else format(p.buy_amt_native, "f"), p.supply_pct,
             p.buy_basis, None if p.argv is None else jdump(recordable_argv(p.argv)), p.decided_ms,
             p.decide_latency_ms, "planned" if (p.verdict == "launch" and p.mode == "live") else None),
        )


def _update(conn: sqlite3.Connection, launch_id: str, **cols: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    with tx(conn):
        conn.execute(f"UPDATE tweet_launches SET {sets} WHERE launch_id = ?", (*cols.values(), launch_id))


# --------------------------------------------------------------------------------------
# live send
# --------------------------------------------------------------------------------------


def _utc_day_start_ms(now_ms: int) -> int:
    return now_ms - now_ms % 86_400_000


def spend_check(conn: sqlite3.Connection, p: Plan, cfg: Config, now_ms: int) -> str | None:
    """Refusal reason, or None. Counts every live launch that may have spent (incl. ambiguous)."""
    day = _utc_day_start_ms(now_ms)
    rows = fetch_all(conn,
        "SELECT chain, author, tweet_id, decided_ms, buy_amt_native FROM tweet_launches WHERE mode = 'live' "
        "AND state IN ('submitting', 'submitted', 'confirmed', 'ambiguous') AND decided_ms >= ?",
        (min(day, now_ms - cfg.per_author_cooldown_s * 1000),))
    today = [r for r in rows if r["decided_ms"] >= day]
    if len(today) >= cfg.daily_launch_cap:
        return f"daily_launch_cap:{len(today)}>={cfg.daily_launch_cap}"
    spent = sum((Decimal(r["buy_amt_native"] or "0") for r in today if r["chain"] == p.chain.value), Decimal(0))
    cap = cfg.daily_native_cap.get(p.chain)
    if cap is not None and p.buy_amt_native is not None and spent + p.buy_amt_native > cap:
        return f"daily_native_cap:{p.chain.value}:{spent}+{p.buy_amt_native}>{cap}"
    if cfg.per_author_cooldown_s > 0 and any(
           r["author"] == p.author and r["tweet_id"] != p.tweet_id
           and r["decided_ms"] >= now_ms - cfg.per_author_cooldown_s * 1000
           for r in rows):
        return f"author_cooldown:{p.author}"
    return None


def _admission(conn: sqlite3.Connection, p: Plan, cfg: Config, lane: Any) -> str | None:
    """Every entry brake except the per-trade size caps, plus the keyless Vanguard pre-flight.

    ``RiskGate.check_entry`` also enforces ``max_position_base_units`` -- 0.1 SOL / 0.156 BNB
    / 0.037 ETH on the box 2026-10-07 -- which refuses every 5% dev buy (1.485 SOL / 0.293
    BNB / 0.089 ETH). The owner asked for 5% (2026-10-06) knowing it is above the flat cap;
    this lane is bounded instead by ``max_buy_native``, ``daily_native_cap``,
    ``daily_launch_cap`` and one unprotected launch per chain at a time.
    """
    risk = get_risk()
    if p.chain not in risk.lane(lane).chains:
        return f"risk:lane_chain_not_enabled:{p.chain.value}"
    tripped = _chain_tripped(conn, p.chain)
    if tripped:
        return f"receipt_breaker:{tripped}"
    if p.chain is Chain.SOL and p.dex == "pump":
        from kaiba.execution import launch_preflight as lp
        glob, _note = lp.read_pump_global(conn)
        why = lp.pump_preflight(glob, route_for(cfg, p.chain, p.dex).holder_fee_args)
        if why:
            return why
    return _risk_brakes(conn, p.chain)


#: kv key that switches a chain's launches off after a failed receipt check (a human clears it).
BREAKER_PREFIX = "tweet_launch:breaker:"


def _chain_tripped(conn: sqlite3.Connection, chain: Chain) -> str | None:
    r = fetch_one(conn, "SELECT value FROM kv WHERE key = ?", (f"{BREAKER_PREFIX}{chain.value}",))
    return r["value"] if r and r["value"] else None


def _trip_chain(conn: sqlite3.Connection, chain: Chain, why: str) -> None:
    at = int(time.time() * 1000)
    with tx(conn):
        conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
            "SET value = excluded.value, updated_ms = excluded.updated_ms",
            (f"{BREAKER_PREFIX}{chain.value}", why[:300], at),
        )
    try:
        from kaiba.core import events as ev
        from kaiba.core.schemas import EventKind
        ev.emit(EventKind.RISK_HALT, {"reason": f"tweet_launch_receipt:{why}"[:300]},
                chain=chain, level="error")
    except Exception:  # noqa: BLE001 - the kv row is the breaker; the event is the page
        log.exception("tweet-launch breaker event")


def _risk_brakes(conn: sqlite3.Connection, chain: Chain) -> str | None:
    """The brakes ``RiskGate.check_entry`` applies that ``_check_mode`` does not: the chain's
    real-money switch, the day's halt (daily loss stop, protection overrun, blind book, an
    operator halt) and the realised daily loss stop. Not the sizer: the dev buy is sized by
    the owner's supply share and capped here by ``max_buy_native`` / ``daily_native_cap``.
    """
    from kaiba.execution.risk import RiskGate

    risk = get_risk()
    budget = risk.chain_budget(chain)
    if not budget.enabled:
        return f"risk:chain_disabled:{chain.value}"
    gate = RiskGate(risk_provider=lambda: risk)
    state = gate._state_row(conn)
    if int(state["halted"]):
        return f"risk:halted:{state['halt_reason'] or 'unspecified'}"
    stop = budget.daily_loss_stop_base_units
    if stop > 0 and gate.realized_today(chain, conn) <= -stop:
        return "risk:daily_loss_stop"
    return None


def _order_id_of(resp: dict[str, Any]) -> str | None:
    for src in (resp, resp.get("data") or {}):
        if isinstance(src, dict) and src.get("order_id"):
            return str(src["order_id"])
    return None


def _report(resp: dict[str, Any]) -> dict[str, Any]:
    src = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    return (src or {}).get("report") or {}


def _atoms(amount: Decimal, chain: Chain) -> int:
    from kaiba.core.schemas import NATIVE_DECIMALS
    return int(amount * (Decimal(10) ** NATIVE_DECIMALS[chain]))


def send(conn: sqlite3.Connection, p: Plan, cfg: Config, *, poll_s: float = 2.0,
         poll_for_s: float = 90.0) -> None:
    """Create the token through GMGN, then hand the dev buy to reconcile as a SUBMITTED order.

    Refusal before the CLI runs -> state 'failed'. Anything after it may have reached the
    chain -> 'ambiguous' at worst, NEVER retried here (a retry launches a second token).
    """
    from kaiba.core.limiter import Priority
    from kaiba.core.schemas import EVM_ZERO, SOL_NATIVE_MINT, Lane, LaneMode, Order, Side
    from kaiba.execution import executor as ex

    _r = route_for(cfg, p.chain, p.dex)
    if not (cfg.live and _r is not None and _r.live) or p.verdict != "launch":
        raise RuntimeError("send() called for a plan that is not armed live")
    try:
        lane = Lane(LANE_VALUE)
    except ValueError:
        _update(conn, p.launch_id, state="failed", error="lane tweet-launch not in kaiba.core.schemas")
        return
    wallet = get_risk().chain_budget(p.chain).wallet
    if not wallet:
        _update(conn, p.launch_id, state="failed", error=f"no wallet for {p.chain.value} in risk.yaml")
        return
    native = SOL_NATIVE_MINT if p.chain is Chain.SOL else EVM_ZERO
    amount_in = _atoms(p.buy_amt_native or Decimal(0), p.chain)
    probe = Order(order_id="probe", chain=p.chain, token="pending", side=Side.BUY, lane=lane,
                  mode=LaneMode.LIVE, input_token=native, output_token="pending",
                  amount_in=amount_in, min_out=0, slippage_bps=cfg.slippage_pct * 100)
    # Admission is resolved before locking SQLite (a pump.fun pre-flight reads the chain).
    row = fetch_one(conn, "SELECT state FROM tweet_launches WHERE launch_id = ?", (p.launch_id,))
    if row is None or row["state"] != "planned":
        return
    admission = _admission(conn, p, cfg, lane)
    # Codex integration candidate: short atomic launch reservation, no provider I/O.
    post = _post_for(conn, p.tweet_id)
    argv = build_argv(p, post, cfg, wallet)
    with tx(conn):
        row = fetch_one(conn, "SELECT state FROM tweet_launches WHERE launch_id = ?", (p.launch_id,))
        if row is None or row["state"] != "planned":
            return  # Restart/repeated send cannot create a second token.
        now = int(time.time() * 1000)
        age = None if post.created_ms is None else now - post.created_ms
        refusal = admission
        if refusal is None and (cfg.dev_buy_supply_pct != 5.0 or p.supply_pct != 5.0):
            refusal = "five_percent_target_required"
        if refusal is None and (age is None or age < 0 or age > cfg.max_tweet_age_s * 1000):
            refusal = "invalid_or_stale_at_send"
        if refusal is None:
            refusal = spend_check(conn, p, cfg, now)
        if refusal is None:
            pending = fetch_one(conn,
                "SELECT launch_id FROM tweet_launches l WHERE mode = 'live' AND chain = ? AND tweet_id != ? "
                "AND (state IN ('submitting','submitted','ambiguous') OR "
                "(state = 'confirmed' AND NOT EXISTS "
                "(SELECT 1 FROM positions b WHERE b.token = l.token AND b.chain = l.chain "
                "AND b.mode != 'shadow' AND (b.protected = 1 OR b.closed_ms IS NOT NULL)))) "
                "LIMIT 1", (p.chain.value, p.tweet_id))
            if pending is not None:
                refusal = "launch_exposure_unresolved"
        if refusal is None:
            try:
                ex._check_mode(probe)
            except ex.ExecutionRefused as exc:
                refusal = f"risk:{exc}"
        if refusal is None:
            refusal = _risk_brakes(conn, p.chain)
        if refusal:
            _update(conn, p.launch_id, state="failed", error=refusal)
            return
        _update(conn, p.launch_id, state="submitting", argv_json=jdump(recordable_argv(argv)))
    try:
        with ex._guarded_patiently("gmgn", "cooking.create", Priority.ENTRY, conn, 5.0):
            # Limiter waits and quote/admission latency count toward tweet freshness.
            ex._check_mode(probe)
            admission = _risk_brakes(conn, p.chain)
            if admission:
                _update(conn, p.launch_id, state="failed", error=admission)
                return
            now = int(time.time() * 1000)
            if post.created_ms is None or not 0 <= now - post.created_ms <= cfg.max_tweet_age_s * 1000:
                _update(conn, p.launch_id, state="failed", error="invalid_or_stale_at_broadcast")
                return
            resp = ex._run_gmgn(argv, timeout_s=60, mutating=True)
    except ex.ExecutionRefused as exc:
        _update(conn, p.launch_id, state="failed", error=str(exc)[:500])
        return
    except Exception as exc:  # noqa: BLE001 - ambiguous: may be on chain
        _update(conn, p.launch_id, state="ambiguous", error=f"{type(exc).__name__}: {str(exc)[:480]}")
        return
    oid = _order_id_of(resp)
    if not oid:
        _update(conn, p.launch_id, state="ambiguous", error="create answered without an order_id")
        return
    _update(conn, p.launch_id, state="submitted", provider_order_id=oid)
    _await_token(conn, p.launch_id, p.chain, oid, amount_in, lane, cfg, poll_s, poll_for_s)


def _await_token(conn, launch_id, chain, oid, amount_in, lane, cfg, poll_s, poll_for_s) -> bool:
    """Poll ``order get`` for the created token; once known, write the SUBMITTED order row."""
    from kaiba.core.limiter import Priority
    from kaiba.core.schemas import EVM_ZERO, SOL_NATIVE_MINT, LaneMode, Order, OrderState, Side
    from kaiba.execution import executor as ex

    deadline = time.monotonic() + poll_for_s
    token = None
    report: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            with ex._guarded_patiently("gmgn", "trade.query_order", Priority.UNRESOLVED, conn, 5.0):
                resp = ex._run_gmgn(["order", "get", "--chain", chain.value, "--order-id", oid])
            report = _report(resp)
            token = report.get("output_token")
            status = str((resp.get("data") or resp).get("status") or "").lower()
            if status in {"failed", "expired", "cancelled", "canceled", "rejected"}:
                _update(conn, launch_id, state="failed", error=f"venue status {status}")
                return False
            if token and status in {"confirmed", "success", "succeeded", "filled", "processed"}:
                break
            token = None
        except Exception as exc:  # noqa: BLE001 - keep polling; the create may still land
            log.warning("order get %s: %s", oid, type(exc).__name__)
        time.sleep(poll_s)
    if not token:
        _update(conn, launch_id, error="token not reported yet; observer keeps polling")
        return False
    native = SOL_NATIVE_MINT if chain is Chain.SOL else EVM_ZERO
    order = Order(
        order_id="ord:" + digest({"tl": launch_id, "o": oid})[:20], decision_id=launch_id,
        chain=chain, token=token, side=Side.BUY, lane=lane, mode=LaneMode.LIVE,
        input_token=native, output_token=token, amount_in=amount_in, min_out=0,
        slippage_bps=cfg.slippage_pct * 100, provider="gmgn", provider_order_id=oid,
    )
    ex._transition(order, OrderState.SUBMITTED, conn, f"tweet launch {launch_id}: cooking create")
    _update(conn, launch_id, state="confirmed", token=token, order_id=order.order_id, error=None)
    _receipt_check(conn, launch_id, chain, report)
    # Our own buy is the first trade. The ladder's silence rule counts from here, so with no
    # other trade the whole bag is sold about stale_no_volume_exit_s after the launch.
    mark_activity(conn, chain, token)
    return True


def _receipt_check(conn: sqlite3.Connection, launch_id: str, chain: Chain, report: dict[str, Any]) -> None:
    """Did the dev buy receive what the curve promised? If not, switch the chain off.

    The position is still booked (the ladder must be able to sell what we hold); only NEW
    launches on that chain stop, until a human deletes the breaker row.
    """
    from kaiba.execution import launch_preflight as lp

    row = fetch_one(conn, "SELECT supply_pct FROM tweet_launches WHERE launch_id = ?", (launch_id,))
    try:
        received = int(report.get("output_amount")) if report.get("output_amount") is not None else None
    except (TypeError, ValueError):
        received = None
    decimals = 6 if chain is Chain.SOL else 18
    ok, note = lp.dev_buy_receipt_check(chain, row["supply_pct"] if row else None, received, decimals)
    _update(conn, launch_id, error=None if ok else f"receipt:{note}")
    if ok is False:
        _trip_chain(conn, chain, f"{launch_id} {note}")
        log.error("tweet-launch receipt check FAILED %s: %s -- %s launches switched off", launch_id, note, chain.value)


# --------------------------------------------------------------------------------------
# activity: "is anyone trading our token" -> the ladder's no-volume exit
# --------------------------------------------------------------------------------------

#: Same key the watchdog reads in ``_last_trade_ms`` (watchdog.TOKEN_ACTIVITY_PREFIX).
ACTIVITY_PREFIX = "token_activity:"


def mark_activity(conn: sqlite3.Connection, chain: Chain, token: str, at_ms: int | None = None) -> None:
    at = at_ms if at_ms is not None else int(time.time() * 1000)
    with tx(conn):
        conn.execute(
            "INSERT INTO kv (key, value, updated_ms) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
            "SET value = excluded.value, updated_ms = excluded.updated_ms",
            (f"{ACTIVITY_PREFIX}{chain.value}:{token}", str(at), at),
        )


def curve_fingerprint(chain: Chain, token: str, conn: Any = None) -> tuple[Any, str]:
    """Something that changes iff the token's curve traded: (fingerprint or None, note).

    A bonding curve moves ONLY on a trade, so its reserves are an exact activity signal for a
    token nobody else tracks. ("graduated",) once the curve is complete: trading moved to a
    DEX pool we do not fingerprint, and a graduation is the opposite of "no volume".
    """
    from kaiba.core.limiter import Priority
    if chain is Chain.SOL:
        from kaiba.execution import snipe
        from kaiba.ingest import launch_feed as lf
        fields, note = snipe.read_pump_curve_fields(conn, lf.pump_curve_address(token))
        if fields is None:
            return None, note
        if fields.get("complete"):
            return ("graduated",), "complete"
        return (fields["real_sol"], fields["real_token"]), "ok"
    from kaiba.execution import evm_price as ep
    if chain is Chain.BSC:
        rpc = ep.json_rpc_batch(Chain.BSC, conn=conn, priority=Priority.EXIT, endpoint="rpc.tweet_launch_activity")
        priced, note = ep.read_flap(token, rpc)
    elif chain is Chain.ROBINHOOD:
        priced, note = ep.read_pons(token, ep.robinhood_rpc_batch(conn=conn, priority=Priority.EXIT))
    else:
        return None, f"no_reader:{chain.value}"
    if priced is None:
        return (("graduated",), note) if "graduat" in str(note) else (None, note)
    cur = priced.curve
    if cur is not None:
        return (cur.tokens_sold_atoms, priced.quote_reserve_base), "ok"
    return (priced.quote_reserve_base, str(priced.price_quote_per_token)), "ok"


def watch_activity(conn: sqlite3.Connection, seen: dict[str, Any], *, now_ms: int | None = None,
                   fingerprint: Any = None) -> int:
    """For every open position we launched, write activity when its curve moved."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    rows = fetch_all(conn,
        "SELECT DISTINCT l.chain, l.token FROM tweet_launches l JOIN positions p "
        "ON p.chain = l.chain AND p.token = l.token AND p.lane = ? "
        "WHERE l.mode = 'live' AND l.token IS NOT NULL AND p.closed_ms IS NULL", (LANE_VALUE,))
    n = 0
    fp_of = fingerprint or curve_fingerprint
    for r in rows:
        chain, token = Chain(r["chain"]), r["token"]
        fp, _note = fp_of(chain, token, conn)
        if fp is None:
            continue                                   # unreadable is not silence: no write
        key = f"{chain.value}:{token}"
        if fp == ("graduated",) or (key in seen and seen[key] != fp):
            mark_activity(conn, chain, token, now)
            n += 1
        seen[key] = fp
    return n


def _activity_own_conn(seen: dict[str, Any]) -> None:
    c = connect()
    try:
        watch_activity(c, seen)
    finally:
        c.close()


def _post_for(conn: sqlite3.Connection, tweet_id: str) -> XPost:
    r = fetch_one(conn, "SELECT * FROM tweet_launch_tweets WHERE tweet_id = ?", (tweet_id,))
    if r is None:
        raise RuntimeError(f"tweet {tweet_id} not recorded")
    return XPost(tweet_id=r["tweet_id"], author=r["author"], text=r["text"] or "", kind=r["kind"] or "post",
                 media=tuple(jload(r["media_json"], [])), created_ms=r["created_ms"],
                 received_ms=r["received_ms"], feed_delay_ms=r["feed_delay_ms"], backend=r["backend"])


# --------------------------------------------------------------------------------------
# market observation: who else launched on this post
# --------------------------------------------------------------------------------------


def observe_once(conn: sqlite3.Connection, cfg: Config, now_ms: int | None = None) -> int:
    """Fill ``competitors_json`` for every horizon that has passed. One short query each."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    longest = max(cfg.observe_horizons_s) * 1000
    rows = fetch_all(conn,
        "SELECT l.launch_id, l.chain, l.symbol, l.name, l.token, l.competitors_json, "
        "COALESCE(t.created_ms, l.decided_ms) AS t0 FROM tweet_launches l "
        "LEFT JOIN tweet_launch_tweets t ON t.tweet_id = l.tweet_id "
        "WHERE l.symbol IS NOT NULL AND l.decided_ms >= ? AND l.observed_ms IS NULL",
        (now - longest - 3_600_000,))
    n = 0
    for r in rows:
        comp = jload(r["competitors_json"], {}) or {}
        changed = False
        for h in cfg.observe_horizons_s:
            if str(h) in comp or now < r["t0"] + h * 1000:
                continue
            toks = fetch_all(conn,
                "SELECT address, symbol, name, launchpad, created_ms, creator FROM tokens "
                "WHERE chain = ? AND created_ms BETWEEN ? AND ? AND (UPPER(symbol) = ? OR UPPER(name) = ?) "
                "ORDER BY created_ms LIMIT 200",
                (r["chain"], r["t0"], r["t0"] + h * 1000, r["symbol"].upper(), (r["name"] or "").upper()))
            comp[str(h)] = {
                "n": len(toks), "ours": r["token"],
                "first": toks[0] if toks else None,
                "tokens": [t["address"] for t in toks[:50]],
            }
            changed = True
        if changed:
            done = all(str(h) in comp for h in cfg.observe_horizons_s)
            _update(conn, r["launch_id"], competitors_json=jdump(comp),
                    observed_ms=(r["t0"] + longest) if done else None)
            n += 1
    return n


# --------------------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------------------


def handle_post(conn: sqlite3.Connection, post: XPost, cfg: Config, *,
                namer: Any = None, logo_maker: Any = None) -> list[Plan]:
    """Record the post, let the model read it, make a logo if needed, plan, record.

    ``namer`` / ``logo_maker`` default to :mod:`kaiba.execution.tweet_creative`; tests pass
    stubs. The model and the logo are only consulted for a post that passes the cheap
    filters (watched author, allowed kind, fresh enough) -- never for a reply or a repost.
    """
    if post.author not in cfg.accounts:
        return []
    if not record_post(conn, post):
        return []                                    # seen before (restart / duplicate delivery)
    from kaiba.execution import tweet_creative as tc

    ai = None
    logo = None
    first = plan_post(post, cfg)
    cheap_fail = ("post_kind_or_empty", "too_late", "invalid_publication_time", "no_route")
    worth_it = any(not any(r.startswith(cheap_fail) for r in p.reasons) for p in first)
    if worth_it:
        if cfg.namer_enabled:
            ai = (namer or tc.pick_identity)(post.text, post.author, backends=cfg.namer_backends,
                                             model=cfg.namer_model, timeout_s=cfg.namer_timeout_s,
                                             hermes_profile=cfg.hermes_profile, majors=MAJORS)
        ident_name = ai.name if ai else (first[0].name if first else None)
        ident_sym = ai.symbol if ai else (first[0].symbol if first else None)
        if cfg.logo_generate and (ai is None or ai.launch):
            # make_logo keeps the post's image only when it is logo-shaped; a screenshot, banner
            # or video frame is replaced by a generated (or drawn) logo.
            logo = (logo_maker or tc.make_logo)(post.media, ident_name, ident_sym,
                                                ai.logo_prompt if ai else "", timeout_s=cfg.logo_timeout_s,
                                                post_raw=dict(post.raw))
    plans = plan_post(post, cfg, ai=ai, logo=logo) if worth_it else first
    for p in plans:
        record_plan(conn, p)
        log.info("tweet-launch %s %s %s %s $%s %s", p.mode, p.verdict, p.author, p.chain.value,
                 p.symbol, ",".join(p.reasons))
    return plans


async def run(cfg_path: Path | None = None) -> None:
    from kaiba.ingest import tweet_launch_feed, x_stream

    settings = get_settings()
    key = settings.twitterapi_io_key
    cfg = load_config(cfg_path)
    feed = tweet_launch_feed.load_config(cfg_path or CONFIG_PATH)
    conn = connect()
    log.info("tweet-launch starting: mode=%s accounts=%d", "live" if cfg.live else "shadow", len(cfg.accounts))

    async def observer() -> None:
        while True:
            try:
                observe_once(conn, cfg)
                if cfg.alpha is not None:
                    await asyncio.to_thread(_refs_marks_own_conn, cfg)
                if cfg.live:
                    await asyncio.to_thread(_resume_own_conn, cfg)
            except Exception as exc:  # noqa: BLE001
                log.warning("observer: %s", exc)
            await asyncio.sleep(30)

    async def subscriber() -> None:
        # Subscribing needs an active twitterapi.io Stream plan (the owner buys it). Until
        # then the provider answers "No active monitoring subscription"; keep retrying so
        # the stream starts by itself the moment the plan is active.
        while True:
            try:
                res = await asyncio.to_thread(x_stream.sync_accounts, key, load_config(cfg_path).accounts.keys())
                log.info("x accounts: added=%s already=%d failed=%s", list(res.added), len(res.already),
                         [f"{h}:{why}" for h, why in res.failed][:3])
                await asyncio.sleep(600 if res.failed else 3600)
                continue
            except Exception as exc:  # noqa: BLE001
                log.warning("x account sync: %s", exc)
            await asyncio.sleep(600)

    async def activity() -> None:
        seen: dict[str, Any] = {}
        while True:
            try:
                await asyncio.to_thread(_activity_own_conn, seen)
            except Exception as exc:  # noqa: BLE001
                log.warning("activity: %s", exc)
            await asyncio.sleep(max(1.0, load_config(cfg_path).activity_poll_s))

    obs = asyncio.create_task(observer())
    sub = asyncio.create_task(subscriber()) if feed.sync_monitored_accounts else None
    act = asyncio.create_task(activity())

    async def warm() -> None:
        # Keep the pump.fun Global read (launch pre-flight, 60 s cache) warm so a launch never
        # waits on it.
        from kaiba.execution import launch_preflight as lp
        while True:
            try:
                await asyncio.to_thread(lp.read_pump_global)
            except Exception as exc:  # noqa: BLE001
                log.warning("warm: %s", exc)
            await asyncio.sleep(30)

    wrm = asyncio.create_task(warm())

    async def vamper() -> None:
        while True:
            try:
                c2 = load_config(cfg_path)
                if c2.vamp_launches in ("live", "shadow"):
                    await asyncio.to_thread(_vamp_own_conn, c2)
            except Exception as exc:  # noqa: BLE001
                log.warning("vamp: %s", exc)
            await asyncio.sleep(5)

    vmp = asyncio.create_task(vamper())
    sends: set[asyncio.Task] = set()
    try:
        async for post in tweet_launch_feed.stream(feed, cfg, api_key=key):
            cfg = load_config(cfg_path)               # edits apply without a restart
            # Naming (model), logo (image API) and the send (GMGN, polls ~90 s) all block;
            # the socket must keep reading, so each post runs in its own thread.
            t = asyncio.create_task(asyncio.to_thread(_process_post, post, cfg))
            sends.add(t)
            t.add_done_callback(sends.discard)
    finally:
        obs.cancel()
        if sub is not None:
            sub.cancel()
        act.cancel()
        vmp.cancel()
        wrm.cancel()


def _process_post(post: XPost, cfg: Config) -> None:
    c = connect()
    try:
        live = [p for p in handle_post(c, post, cfg) if p.verdict == "launch" and p.mode == "live"]
        if len(live) == 1:
            send(c, live[0], cfg)
        elif live:
            # several chains / launchpads for one post: in parallel, each on its own connection,
            # so the second venue is not sent after the post has gone stale.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(len(live)) as ex:
                list(ex.map(lambda p: _send_own_conn(p, cfg), live))
    except Exception:  # noqa: BLE001 - recorded where possible; never crash the stream
        log.exception("tweet-launch post %s", post.tweet_id)
    try:
        if cfg.alpha is not None and post.author in cfg.alpha.accounts:
            from kaiba.execution import tweet_refs
            tweet_refs.handle_refs(c, post, cfg.alpha, cfg)
    except Exception:  # noqa: BLE001 - measurement must never take the launcher down
        log.exception("tweet-refs post %s", post.tweet_id)
    finally:
        c.close()


def _vamp_own_conn(cfg: Config) -> None:
    c = connect()
    try:
        vamp_scan(c, cfg)
    finally:
        c.close()


def vamp_scan(conn: sqlite3.Connection, cfg: Config, *, now_ms: int | None = None, info: Any = None) -> int:
    """Clone a launch others made from a watched post, once it shows volume.

    Candidates: tokens on the post's chain created after the post that either link the post
    (``tweet_sources``) or carry the ticker our picker derives from it. ``tweet_vamp`` (Codex)
    decides: fresh post, provider-reported 5-minute volume and swaps > 0, the post names the
    token by contract or exact name+ticker, not one of ours. The vamp takes the same
    ``tl:<tweet>:<chain>`` slot as our own launch, so a post is launched at most once per chain,
    and goes through the same ``send`` (every brake, 5% buy, holder fees, ladder exits).
    MEASURED prior: copycats graduate 0.86% vs 9.20% for originals (arXiv 2609.10246).
    """
    from dataclasses import replace

    from kaiba.core.limiter import Priority
    from kaiba.core.schemas import Receipt
    from kaiba.execution import tweet_vamp as tv

    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if cfg.vamp_launches not in ("live", "shadow") or not cfg.accounts:
        return 0
    vcfg = replace(cfg, max_tweet_age_s=cfg.vamp_window_s,
                   mode=cfg.mode if cfg.vamp_launches == "live" else "shadow")
    marks = ",".join("?" for _ in cfg.accounts)
    posts = fetch_all(conn,
        f"SELECT tweet_id FROM tweet_launch_tweets WHERE created_ms >= ? AND author IN ({marks})",
        (now - cfg.vamp_window_s * 1000, *cfg.accounts))
    if not posts:
        return 0
    owned = frozenset(r["token"] for r in fetch_all(conn, "SELECT token FROM tweet_launches WHERE token IS NOT NULL"))
    if info is None:
        from kaiba.providers import gmgn_cli
        info = gmgn_cli.token_info
    sent = 0
    for row in posts:
        post = _post_for(conn, row["tweet_id"])
        ident = derive_identity(post.text)
        for chain in cfg.accounts.get(post.author, ()):
            done = fetch_one(conn, "SELECT verdict, state FROM tweet_launches WHERE launch_id = ?",
                             (f"tl:{post.tweet_id}:{chain.value}",))
            if done is not None and (done["verdict"] == "launch" or done["state"] is not None):
                continue
            cands = fetch_all(conn,
                "SELECT t.address FROM tokens t LEFT JOIN tweet_sources s ON s.chain = t.chain AND s.token = t.address "
                "WHERE t.chain = ? AND t.created_ms >= ? AND (s.tweet_id = ? OR UPPER(t.symbol) = ?) "
                "ORDER BY t.created_ms LIMIT 3",
                (chain.value, post.created_ms or now, post.tweet_id, ident.symbol if ident else ""))
            for cand_row in cands:
                addr = cand_row["address"]
                if addr in owned:
                    continue
                try:
                    r = info(addr, chain, priority=Priority.ENTRY)
                    if not getattr(r, "ok", False):
                        continue
                    cand = tv.from_gmgn(chain, addr, r.data, Receipt(provider="gmgn", endpoint="token.info",
                                                                       observed_at_ms=int(time.time() * 1000)))
                    plan = tv.plan_vamp(post, cand, vcfg, now_ms=int(time.time() * 1000),
                                        min_volume_usd=cfg.vamp_min_volume_usd, owned_tokens=owned)
                except ValueError as exc:
                    log.info("vamp skip %s %s %s: %s", post.author, chain.value, addr, exc)
                    continue
                if not tv.record_vamp(conn, post, plan):
                    break
                log.info("vamp %s %s %s $%s from %s", plan.mode, post.author, chain.value, plan.symbol, addr)
                if plan.mode == "live":
                    send(conn, plan, vcfg)
                    sent += 1
                break
    return sent


def _refs_marks_own_conn(cfg: Config) -> None:
    from kaiba.execution import tweet_refs
    c = connect()
    try:
        tweet_refs.mark_due(c, cfg.alpha)
    finally:
        c.close()


def _send_own_conn(p: Plan, cfg: Config) -> None:
    c = connect()
    try:
        send(c, p, cfg)
    except Exception:  # noqa: BLE001 - recorded on the row; never crash the stream
        log.exception("tweet-launch send %s", p.launch_id)
    finally:
        c.close()


def _resume_own_conn(cfg: Config) -> None:
    c = connect()
    try:
        resume_pending(c, cfg)
    finally:
        c.close()


def resume_pending(conn: sqlite3.Connection, cfg: Config) -> int:
    """Live launches GMGN accepted whose token was not reported in time: ask again.
    Without this the dev bag would exist on chain with no position and no stop."""
    from kaiba.core.schemas import Lane

    rows = fetch_all(conn,
        "SELECT launch_id, chain, provider_order_id, buy_amt_native FROM tweet_launches "
        "WHERE mode = 'live' AND state = 'submitted' AND token IS NULL AND provider_order_id IS NOT NULL")
    n = 0
    for r in rows:
        chain = Chain(r["chain"])
        if _await_token(conn, r["launch_id"], chain, r["provider_order_id"],
                        _atoms(Decimal(r["buy_amt_native"] or "0"), chain), Lane(LANE_VALUE),
                        cfg, 2.0, 6.0):
            n += 1
    return n


def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m kaiba.execution.tweet_launch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="stream watched accounts; shadow-record or launch")
    sub.add_parser("sync-accounts", help="subscribe config accounts on twitterapi.io")
    t = sub.add_parser("plan", help="dry-run one post text through the planner (no I/O)")
    t.add_argument("--author", required=True)
    t.add_argument("--text", required=True)
    t.add_argument("--media", default="")
    t.add_argument("--kind", default="post")
    sub.add_parser("report", help="summary of recorded tweets and plans")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.cmd == "run":
        asyncio.run(run())
        return 0
    if a.cmd == "sync-accounts":
        from kaiba.ingest import x_stream
        cfg = load_config()
        res = x_stream.sync_accounts(get_settings().twitterapi_io_key, cfg.accounts.keys())
        print(json.dumps(asdict(res), indent=2))
        return 0 if not res.failed else 1
    if a.cmd == "plan":
        now = int(time.time() * 1000)
        post = XPost(tweet_id=str(now), author=a.author.lower().lstrip("@"), text=a.text, kind=a.kind,
                     media=tuple(x for x in a.media.split(",") if x), created_ms=now, received_ms=now,
                     feed_delay_ms=None, backend="cli")
        for p in plan_post(post, load_config(), now_ms=now):
            d = asdict(p)
            d["chain"] = p.chain.value
            d["buy_amt_native"] = None if p.buy_amt_native is None else str(p.buy_amt_native)
            print(json.dumps(d, indent=2))
        return 0
    if a.cmd == "report":
        conn = connect()
        try:
            for r in fetch_all(conn,
                    "SELECT chain, mode, verdict, COUNT(*) n, MIN(decide_latency_ms) best_ms "
                    "FROM tweet_launches GROUP BY chain, mode, verdict"):
                print(r)
        finally:
            conn.close()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
