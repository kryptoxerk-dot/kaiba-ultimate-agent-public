"""Codex-owned isolated UI preview. Synthetic data only; no core/settings/DB imports.

Run: .venv/Scripts/python -m uvicorn dashboard.terminal_demo:app --host 127.0.0.1 --port 8766
Paper orders live in browser memory and disappear on reload. This preview has no POST,
provider, shell, execution, risk config, credential or production DB route.
"""

from __future__ import annotations

import math
import random
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def demo_snapshot() -> dict:
    now = int(time.time() * 1000)
    end = now // 900_000 * 900_000
    specs = [
        ("SOL", "Solana", "sol", 180.24, 2.31, 84_200_000_000, 320_000_000, 3_180_000_000, "solana"),
        ("ETH", "Ethereum", "eth", 3420.18, 1.12, 411_000_000_000, 1_200_000_000, 12_100_000_000, "ethereum"),
        ("BNB", "BNB Chain", "bsc", 587.36, -0.48, 85_400_000_000, 218_000_000, 846_000_000, "bnb"),
        ("BONK", "Bonk", "sol", .00002418, 5.24, 1_820_000_000, 42_600_000, 218_400_000, "bonk"),
        ("WIF", "dogwifhat", "sol", .7342, -2.11, 733_800_000, 18_400_000, 87_300_000, "wif"),
        ("POPCAT", "Popcat", "sol", 1.238, 3.76, 1_210_000_000, 12_100_000, 64_900_000, "popcat"),
        ("JUP", "Jupiter", "sol", .9124, .58, 3_020_000_000, 28_400_000, 98_200_000, "jup"),
    ]
    tokens = []
    for i, (symbol, name, chain, price, change, cap, liq, volume, slug) in enumerate(specs):
        rng = random.Random(719 + i)
        candles = []
        previous = .94
        for j in range(384):
            close = .94 + .00017 * j + .026 * math.sin(j / 13 + i) + .018 * math.sin(j / 4.9) + rng.uniform(-.009, .009)
            candles.append({"time": end - (383 - j) * 900_000, "open": previous,
                            "high": max(previous, close) + rng.uniform(.002, .012),
                            "low": min(previous, close) - rng.uniform(.002, .012),
                            "close": close, "volume": rng.randint(1000, 11000)})
            previous = close
        # Tie the illustrative 24h change to the same candle series, not an unrelated label.
        correction = math.log((1 + change / 100) / (candles[-1]["close"] / candles[-97]["close"])) / 96
        for j, candle in enumerate(candles):
            factor = math.exp(correction * j)
            for field in ("open", "high", "low", "close"):
                candle[field] *= factor
        ratio = price / candles[-1]["close"]
        for candle in candles:
            for field in ("open", "high", "low", "close"):
                candle[field] *= ratio
        tokens.append({"id": slug, "address": None, "symbol": symbol, "name": name,
                       "chain": chain, "price": price, "change": change, "market_cap": cap,
                       "liquidity": liq, "volume": volume, "candles": candles, "observed_ms": now})
    return {
        "demo": True, "mode": "demo", "tokens": tokens, "generated_ms": now, "errors": {},
        "scope": "Illustrative market data · not current prices or actual performance",
        "balances": [{"symbol": "SOL", "qty": 1.245, "price": 180.24, "chain": "sol"},
                     {"symbol": "ETH", "qty": .05, "price": 3420.18, "chain": "eth"},
                     {"symbol": "BNB", "qty": .12, "price": 587.36, "chain": "bsc"}],
        "positions": [
            {"id": "demo-bonk", "token_id": "bonk", "symbol": "BONK", "chain": "sol", "mode": "demo",
             "lane": "Illustrative", "qty": 2_400_000, "entry": .00002156, "mark": .00002418,
             "pnl": 6.288, "protection": "TP / SL · simulated", "stop": .000020538},
            {"id": "demo-wif", "token_id": "wif", "symbol": "WIF", "chain": "sol", "mode": "demo",
             "lane": "Illustrative", "qty": 100, "entry": .7658, "mark": .7342,
             "pnl": -3.16, "protection": "Stop · simulated", "stop": .62407},
            {"id": "demo-popcat", "token_id": "popcat", "symbol": "POPCAT", "chain": "sol", "mode": "demo",
             "lane": "Illustrative", "qty": 20, "entry": 1.134, "mark": 1.238,
             "pnl": 2.08, "protection": "Trailing · simulated", "stop": 1.0523},
        ],
        "events": [
            {"time": now - 4_000, "title": "Risk check completed", "detail": "BONK · illustrative size within demo limits", "kind": "risk"},
            {"time": now - 9_000, "title": "Quote received", "detail": "BONK / USD · sample price, not executable", "kind": "quote"},
            {"time": now - 15_000, "title": "Paper position observed", "detail": "Simulated position · no order sent", "kind": "paper"},
        ],
    }


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    from fastapi.responses import JSONResponse

    if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        return JSONResponse({"error": "loopback_only"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
@app.get("/terminal", response_class=HTMLResponse)
def terminal_preview(request: Request):
    return templates.TemplateResponse(request=request, name="terminal.html", context={"snapshot": demo_snapshot()})


@app.get("/api/terminal")
def terminal_data():
    return demo_snapshot()
