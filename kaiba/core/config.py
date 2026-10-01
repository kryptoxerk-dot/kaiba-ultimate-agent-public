"""Configuration: environment secrets plus the YAML risk envelope.

Two different things live here and they must not be confused.

``Settings`` is credentials and endpoints, read from ``.env`` / the process environment.
``RiskConfig`` is the operating envelope in ``config/risk.yaml``: what the agent is allowed
to do with money. Hermes may move numbers inside the envelope; it may not widen the
envelope itself, because the bounds file is owned by the operator and is loaded read-only.

The one rule that is not a number anywhere in here is withdrawal. There is no
"withdrawals: true" switch to flip — the signer policy has no code path that signs a
transfer to a non-owned address, and the GMGN API has no transfer endpoint at all.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kaiba.core.schemas import Chain, Lane, LaneMode

REPO_ROOT = Path(__file__).resolve().parents[2]
USER_CONFIG_DIR = Path(os.environ.get("KAIBA_CONFIG_DIR", Path.home() / ".config" / "kaiba"))


def _env_files() -> tuple[str, ...]:
    """Repo ``.env`` first, then ``~/.config/kaiba/.env`` (later wins on conflict)."""
    return (str(REPO_ROOT / ".env"), str(USER_CONFIG_DIR / ".env"))


class Settings(BaseSettings):
    """Secrets and endpoints. Never log an instance of this."""

    model_config = SettingsConfigDict(
        env_file=_env_files(), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # runtime
    kaiba_env: str = "dev"
    kaiba_data_dir: Path = REPO_ROOT / "data"
    kaiba_db_path: Path | None = None
    kaiba_log_level: str = "INFO"
    kaiba_dashboard_host: str = "127.0.0.1"
    kaiba_dashboard_port: int = 8788
    kaiba_dashboard_password: str = ""

    # telegram
    telegram_bot_token: str = ""
    telegram_bot_username: str = "your_kaiba_bot"
    telegram_allowed_users: str = ""
    telegram_home_channel: str = ""
    tg_api_id: str = ""
    tg_api_hash: str = ""
    tg_session_path: str = "~/.config/kaiba/telethon.session"
    tg_call_channels: str = ""

    # gmgn
    gmgn_api_key: str = ""
    gmgn_private_key: str = ""
    gmgn_cli_path: str = ""
    gmgn_allow_automated_trades: int = 0

    # models
    anthropic_api_key: str = ""
    xai_api_key: str = ""
    openrouter_api_key: str = ""

    # solana data
    helius_api_key: str = ""
    helius_webhook_secret: str = ""
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    solana_tracker_api_key: str = ""
    birdeye_api_key: str = ""
    pumpportal_api_key: str = ""
    jupiter_api_key: str = ""
    bitquery_token: str = ""

    # evm data
    alchemy_api_key: str = ""
    alchemy_webhook_signing_key: str = ""
    robinhood_rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    bsc_rpc_url: str = ""
    base_rpc_url: str = ""
    eth_rpc_url: str = ""
    etherscan_api_key: str = ""
    blockscout_api_key: str = ""

    # safety / enrichment
    goplus_app_key: str = ""
    goplus_app_secret: str = ""
    rugcheck_jwt: str = ""
    coingecko_api_key: str = ""
    nansen_api_key: str = ""
    cielo_api_key: str = ""
    mobula_api_key: str = ""
    codex_io_api_key: str = ""
    twitterscore_api_key: str = ""

    # news
    cryptopanic_api_key: str = ""
    newsapi_key: str = ""

    # analytics and indexers added 2026-09-21 from the operator's key set.
    #
    # `dune_api_key` is the one that changes what we can answer. Four separate research
    # passes this week returned "no public data found" on graduation rates, daily launch
    # counts and per-aggregator routing share -- every time because the source was a Dune
    # dashboard and dune.com is a JavaScript app that a fetcher cannot read. With a key,
    # those queries are an HTTP call.
    dune_api_key: str = ""
    vybe_api_key: str = ""
    goldrush_api_key: str = ""
    goldsky_api_key: str = ""
    moralis_api_key: str = ""
    twitterapi_io_key: str = ""
    finnhub_api_key: str = ""
    fred_api_key: str = ""
    github_api_token: str = ""

    # observability
    phoenix_collector_endpoint: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    @property
    def db_path(self) -> Path:
        return self.kaiba_db_path or (self.kaiba_data_dir / "kaiba.db")

    @property
    def cache_dir(self) -> Path:
        return self.kaiba_data_dir / "cache"

    @property
    def allowed_telegram_users(self) -> list[int]:
        return [int(x) for x in self.telegram_allowed_users.replace(" ", "").split(",") if x]

    @property
    def call_channels(self) -> list[str]:
        return [x.strip() for x in self.tg_call_channels.split(",") if x.strip()]

    def rpc_for(self, chain: Chain) -> str:
        return {
            Chain.SOL: self.solana_rpc_url,
            Chain.ROBINHOOD: self.robinhood_rpc_url,
            Chain.BSC: self.bsc_rpc_url,
            Chain.BASE: self.base_rpc_url,
            Chain.ETH: self.eth_rpc_url,
        }.get(chain, "")

    def present(self) -> dict[str, bool]:
        """Which credentials exist. Names and booleans only — never values."""
        keys = [
            "telegram_bot_token", "tg_api_id", "tg_api_hash", "gmgn_api_key", "gmgn_private_key",
            "helius_api_key", "solana_tracker_api_key", "birdeye_api_key", "jupiter_api_key",
            "bitquery_token", "alchemy_api_key", "etherscan_api_key", "blockscout_api_key",
            "goplus_app_key", "coingecko_api_key", "nansen_api_key", "cielo_api_key",
            "mobula_api_key", "codex_io_api_key", "twitterscore_api_key", "xai_api_key",
            "cryptopanic_api_key", "dune_api_key", "vybe_api_key", "goldrush_api_key",
            "goldsky_api_key", "moralis_api_key", "twitterapi_io_key", "github_api_token",
        ]
        return {k: bool(getattr(self, k)) for k in keys}


# --------------------------------------------------------------------------------------
# risk envelope
# --------------------------------------------------------------------------------------


class ChainBudget(BaseModel):
    """Per-chain money limits, in native base units so there is no float drift."""

    enabled: bool = False
    bankroll_base_units: int = 0
    max_position_base_units: int = 0
    min_position_base_units: int = 0
    gas_reserve_base_units: int = 0
    daily_loss_stop_base_units: int = 0
    max_exposure_pct: float = 10.0
    wallet: str | None = None


class LaneConfig(BaseModel):
    mode: LaneMode = LaneMode.SHADOW
    size_pct_min: float = 0.25
    size_pct_max: float = 2.0
    chains: list[Chain] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class EnvelopeBounds(BaseModel):
    """Hard bounds the agent may not exceed when it retunes itself.

    Owned by the operator. ``kaiba.core.config`` refuses to widen these at runtime.
    """

    max_size_pct_bankroll: float = 5.0
    # Cap on TOTAL open exposure across every position on a chain, as % of bankroll.
    # Added 2026-09-21: max_exposure_pct is per TOKEN and max_concurrent_positions is
    # declared but enforced nowhere, so nothing capped the basket -- a simulated
    # high-volume run admitted 22 simultaneous positions = 97.8% of the bankroll.
    # Positions on one chain in one hour are one factor (pairwise rho of returns +0.22,
    # robust across 15/30/60-min buckets), so ten positions are ~3.4 independent bets and
    # diversification is exhausted by ~8. A TOTAL cap respects the owner's no-count-cap
    # mandate and self-tightens as the bankroll shrinks. 25 is the risk-scaling agent's
    # figure; the ceiling is a judgement, the rho that bounds it is measured.
    max_total_exposure_pct: float = 25.0
    max_daily_loss_pct: float = 10.0
    max_slippage_bps: int = 2500
    #: What an EXIT may give up, against ``max_slippage_bps`` for an entry.
    #:
    #: OWNER 2026-09-24: "Make sure the agent uses 88% slippage anti mev". Scoped to exits,
    #: and the scoping is the whole point -- the two sides want opposite things.
    #:
    #: MEASURED on the live box that day: 97 of 104 failed sells in 24h were the venue
    #: refusing with ``code=400 error=40003702 message=GEvmInsufficientSlippage``. The
    #: floor we send is derived from our last MARK, and on a token that has fallen since
    #: that mark, 75% of a stale number is still far above anything the pool will pay. One
    #: robinhood position demanded 0.0045 ETH for a token with ZERO trades in 24 hours and
    #: retried 90 times. Positions sat 26-41 h unsellable, which is what stopped entries.
    #:
    #: An exit is a stop-loss, a rug escape, a dead-token cleanup. Its job is to be OUT; a
    #: protective sell that cannot execute is worse than one that executes badly. An ENTRY
    #: is the opposite -- at 8800 an entry would accept 12% of the tokens it expected, on
    #: exactly the thin fresh launches this book already loses money on, so entries keep
    #: 2500 and this is deliberately a SEPARATE number rather than a raise of that one.
    #:
    #: Safe to widen only because ``--anti-mev`` is on EVERY swap unconditionally
    #: (executor.py), so a wide floor is not an open invitation to sandwich us.
    max_exit_slippage_bps: int = 8800
    max_concurrent_positions: int | None = None  # null = no count cap, by owner mandate
    max_lane_mode: LaneMode = LaneMode.LIVE
    allow_self_promotion: bool = True
    #: Share of a position one round trip may consume in venue costs
    #: (``kaiba.execution.viability``). Typed here rather than left as a loose YAML key
    #: because this model drops what it does not declare, and ``save_risk`` writes the
    #: model back: an undeclared key survives until the first ``kaiba risk`` command or
    #: self-tune and is then silently deleted. A risk ceiling that disappears when the
    #: agent retunes itself is worse than no ceiling, because nobody is watching for it.
    max_round_trip_cost_pct: float = 7.0

    @field_validator("max_round_trip_cost_pct", mode="before")
    @classmethod
    def _reject_bool_ceiling(cls, value: object) -> object:
        """``true`` is not 1%.

        ``bool`` is a subclass of ``int``, so pydantic quietly coerces a YAML ``true``
        into a 1.0% ceiling -- a real, much tighter limit the operator never wrote. It
        fails safe in direction, which is precisely what makes it dangerous: nothing
        would ever surface it. Refuse the load instead; ``viability`` treats an
        unreadable risk config as no usable ceiling and stops.
        """
        if isinstance(value, bool):
            raise ValueError("max_round_trip_cost_pct must be a number, not a boolean")
        return value


class RiskConfig(BaseModel):
    version: str = "v1"
    global_mode: LaneMode = LaneMode.SHADOW
    kill_switch: bool = False
    entries_paused: bool = False
    reduce_only: bool = False
    bounds: EnvelopeBounds = Field(default_factory=EnvelopeBounds)
    chains: dict[Chain, ChainBudget] = Field(default_factory=dict)
    lanes: dict[Lane, LaneConfig] = Field(default_factory=dict)
    protection: dict[str, Any] = Field(default_factory=dict)
    provider_budgets: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def lane(self, lane: Lane) -> LaneConfig:
        return self.lanes.get(lane, LaneConfig(mode=LaneMode.OFF))

    def chain_budget(self, chain: Chain) -> ChainBudget:
        return self.chains.get(chain, ChainBudget())

    def effective_mode(self, lane: Lane) -> LaneMode:
        """A lane never runs hotter than the global mode or the envelope ceiling."""
        order = [LaneMode.OFF, LaneMode.SHADOW, LaneMode.CANARY, LaneMode.LIVE]
        if self.kill_switch:
            return LaneMode.OFF
        candidates = [self.lane(lane).mode, self.global_mode, self.bounds.max_lane_mode]
        return min(candidates, key=order.index)

    def clamp_size_pct(self, pct: float) -> float:
        return max(0.0, min(pct, self.bounds.max_size_pct_bankroll))


DEFAULT_RISK_PATH = REPO_ROOT / "config" / "risk.yaml"


def load_risk(path: Path | None = None) -> RiskConfig:
    p = path or Path(os.environ.get("KAIBA_RISK_PATH", DEFAULT_RISK_PATH))
    if not p.exists():
        return RiskConfig()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return RiskConfig.model_validate(raw)


def save_risk(cfg: RiskConfig, path: Path | None = None) -> Path:
    """Write the tunable parts back.

    If the file already exists its ``bounds`` block wins, so a self-tune by the agent can
    never widen the operator's envelope. On first creation the caller's bounds are written
    as the bootstrap value.
    """
    p = path or Path(os.environ.get("KAIBA_RISK_PATH", DEFAULT_RISK_PATH))
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        cfg = cfg.model_copy(update={"bounds": load_risk(p).bounds})
    p.write_text(
        yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True), sort_keys=False),
        encoding="utf-8",
    )
    return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_risk() -> RiskConfig:
    """Not cached: the dashboard and Hermes both write this file while the agent runs."""
    return load_risk()


def native_to_decimal(amount: int, chain: Chain) -> Decimal:
    from kaiba.core.schemas import NATIVE_DECIMALS

    return Decimal(amount) / (Decimal(10) ** NATIVE_DECIMALS[chain])
