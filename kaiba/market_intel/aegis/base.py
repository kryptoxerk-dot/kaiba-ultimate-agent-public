"""Collector framework for the Aegis Terminal's market intel.

A collector is a pure function of an ``Http`` object. It returns a JSON-serializable dict, so the
tests run offline on recorded fixtures (``FixtureHttp``) and the live terminal uses ``LiveHttp``.

Rules every collector follows:

* **Public data only.** Keyless public endpoints, plus the owner's own X bearer token when one is
  set. Nothing reads repository secrets, and nothing here can trade or change engine state.
* **Partial failure is data.** A collector that reads several feeds reports per-feed
  ``{"name", "ok", "error", "count"}`` in its payload, so one dead feed does not blank the panel.
* **Whole failure keeps the last good data.** ``run_collector`` then marks it ``stale`` and records
  the error, so the terminal never shows old data as fresh.

Caches are written atomically to ``<AEGIS_TERMINAL_DATA or runtime/terminal>/intel/<key>.json``.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parents[3]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AegisTerminal/2.0 (personal market dashboard)"
MAX_BYTES = 64 * 1024 * 1024          # the Hyperliquid leaderboard is about 40 MB


def data_dir() -> Path:
    return Path(os.environ.get("AEGIS_TERMINAL_DATA") or (REPO / "runtime" / "terminal"))


def cache_dir() -> Path:
    return data_dir() / "intel"


# ------------------------------------------------------------------ http
class Http(Protocol):
    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str: ...
    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any: ...
    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes: ...
    def post_json(self, url: str, body: Any) -> Any: ...


class LiveHttp:
    """httpx with a browser-like user agent, redirects, a size cap and raise-on-error."""

    def __init__(self, timeout: float = 25.0) -> None:
        import httpx

        self._c = httpx.Client(timeout=timeout, follow_redirects=True,
                               headers={"User-Agent": USER_AGENT, "Accept": "*/*"})

    def _get(self, url: str, headers: dict[str, str] | None) -> bytes:
        r = self._c.get(url, headers=headers)
        r.raise_for_status()
        if len(r.content) > MAX_BYTES:
            raise ValueError(f"response too large: {len(r.content)} bytes")
        return r.content

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self._get(url, headers).decode("utf-8", errors="replace")

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        return json.loads(self._get(url, headers))

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        return self._get(url, headers)

    def post_json(self, url: str, body: Any) -> Any:
        r = self._c.post(url, json=body)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._c.close()


class FixtureHttp:
    """Offline Http for tests: the first route whose substring is in the URL answers.

    A route value is a ``Path`` (file contents), ``str``/``bytes`` (body), any other object
    (returned as parsed JSON), or an ``Exception`` instance (raised). POST routes are matched on
    ``url + " " + json.dumps(body, sort_keys=True)``. Unmatched URLs raise ``LookupError``."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def _answer(self, key: str) -> Any:
        self.calls.append(key)
        for sub, val in self.routes.items():
            if sub in key:
                if isinstance(val, Exception):
                    raise val
                if isinstance(val, Path):
                    return val.read_bytes()
                return val
        raise LookupError(f"no fixture for {key}")

    def get_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes:
        v = self._answer(url)
        return v if isinstance(v, bytes) else (v.encode() if isinstance(v, str) else json.dumps(v).encode())

    def get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self.get_bytes(url, headers).decode("utf-8", errors="replace")

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        v = self._answer(url)
        return json.loads(v) if isinstance(v, (bytes, str)) else v

    def post_json(self, url: str, body: Any) -> Any:
        v = self._answer(url + " " + json.dumps(body, sort_keys=True))
        return json.loads(v) if isinstance(v, (bytes, str)) else v


# ------------------------------------------------------------------ helpers shared by collectors
def utc_iso(ts: float | datetime | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_when(text: str | None) -> datetime | None:
    """RFC 822 (RSS), ISO 8601 (Atom/JSON) or epoch seconds/milliseconds; always timezone-aware UTC."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        v = float(s)
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        dt = None
    if dt is None:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def strip_html(text: str | None, limit: int = 0) -> str:
    import html as _html

    s = re.sub(r"<[^>]+>", " ", text or "")
    s = re.sub(r"\s+", " ", _html.unescape(s)).strip()
    return (s[: limit - 1] + "…") if limit and len(s) > limit else s


def num(x: Any) -> float | None:
    """Float or None for '', None, '-', NaN and unparseable values; accepts '1,234' and '(12.5)' (negative)."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return None if math.isnan(x) else float(x)
    s = str(x).strip().replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").strip()
    if s in ("", "-", "—", "–", "N/A", "n/a"):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def safe_xml(text: str) -> str:
    """Refuse XML that declares entities (expat 2.4+ already limits expansion; this is belt and braces)."""
    if re.search(r"<!ENTITY", text[:4096], re.IGNORECASE):
        raise ValueError("XML with entity declarations refused")
    return text


# ------------------------------------------------------------------ collectors and cache
@dataclass(frozen=True)
class Collector:
    key: str
    title: str
    interval_s: int
    run: Callable[[Http], dict[str, Any]]
    sources: tuple[str, ...] = ()


def cache_path(key: str) -> Path:
    if not re.fullmatch(r"[a-z0-9_]+", key):
        raise ValueError(f"bad collector key {key!r}")
    return cache_dir() / f"{key}.json"


def read_cache(key: str) -> dict[str, Any] | None:
    try:
        doc = json.loads(cache_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _write_atomic(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    os.replace(tmp, path)


def run_collector(c: Collector, http: Http, now: float | None = None) -> dict[str, Any]:
    """Run one collector and write its cache. On failure the previous data is kept and marked stale."""
    t0 = time.time()
    now = t0 if now is None else now
    try:
        data = c.run(http)
        doc = {"key": c.key, "title": c.title, "ok": True, "stale": False, "error": None,
               "fetched_at": utc_iso(now), "fetched_ts": now, "duration_s": round(time.time() - t0, 2),
               "interval_s": c.interval_s, "sources": list(c.sources), "data": data}
    except Exception as e:  # noqa: BLE001 - any collector failure becomes visible, never fatal
        prev = read_cache(c.key) or {}
        doc = {"key": c.key, "title": c.title, "ok": False, "stale": bool(prev.get("data")),
               "error": f"{type(e).__name__}: {str(e)[:300]}", "fetched_at": prev.get("fetched_at"),
               "fetched_ts": prev.get("fetched_ts"), "attempted_at": utc_iso(now), "attempted_ts": now,
               "duration_s": round(time.time() - t0, 2), "interval_s": c.interval_s,
               "sources": list(c.sources), "data": prev.get("data")}
    _write_atomic(cache_path(c.key), doc)
    return doc


class Refresher:
    """Runs due collectors in the background (one at a time per collector) and on request."""

    def __init__(self, collectors: list[Collector], http_factory: Callable[[], Http] = LiveHttp,
                 workers: int = 4, tick_s: float = 5.0) -> None:
        self.collectors = {c.key: c for c in collectors}
        self.http_factory = http_factory
        self.tick_s = tick_s
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="intel")
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._forced: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def due(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        out = []
        for key, c in self.collectors.items():
            doc = read_cache(key) or {}
            last = doc.get("attempted_ts") or doc.get("fetched_ts") or 0
            # a failed run retries after a fifth of the interval (at least a minute), not a full one
            wait = c.interval_s if doc.get("ok", True) else max(60, c.interval_s // 5)
            if key in self._forced or now - float(last) >= wait:
                out.append(key)
        return out

    def _run(self, key: str) -> None:
        http = self.http_factory()
        try:
            run_collector(self.collectors[key], http)
        finally:
            close = getattr(http, "close", None)
            if close:
                close()
            with self._lock:
                self._running.discard(key)

    def kick(self, now: float | None = None) -> list[str]:
        started = []
        for key in self.due(now):
            with self._lock:
                if key in self._running:
                    continue
                self._running.add(key)
                self._forced.discard(key)
            self._pool.submit(self._run, key)
            started.append(key)
        return started

    def request(self, key: str | None = None) -> list[str]:
        keys = list(self.collectors) if key is None else [key]
        unknown = [k for k in keys if k not in self.collectors]
        if unknown:
            raise KeyError(unknown[0])
        with self._lock:
            self._forced.update(keys)
        return self.kick()

    def running(self) -> list[str]:
        with self._lock:
            return sorted(self._running)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.kick()
                except Exception:  # noqa: BLE001, S110 - the loop must survive any single bad tick
                    pass
                self._stop.wait(self.tick_s)

        self._thread = threading.Thread(target=loop, name="intel-refresher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
