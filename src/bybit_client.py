from __future__ import annotations
from typing import Any, Dict, List, Optional
import httpx

BASE_URL = "https://api.bybit.com"

class BybitError(RuntimeError):
    pass

class BybitClient:
    """
    Лёгкий клиент Bybit REST v5 для свечей (spot|linear).
    Прокси применяется только здесь.
    """
    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 15.0):
        proxies = None
        if proxy_url:
            proxies = {"http://": proxy_url, "https://": proxy_url}
        self.client = httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(timeout),
            proxies=proxies,
            headers={"User-Agent": "crypto-signal-bot-3.0/step-4"},
        )

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        r = self.client.get(path, params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            raise BybitError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")
        return data

    def klines(self, category: str, symbol: str, interval: str, limit: int = 200) -> List[Dict[str, Any]]:
        category = category.lower()
        if category not in {"spot", "linear"}:
            raise ValueError("category must be 'spot' or 'linear'")
        params = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "limit": str(min(max(limit, 1), 1000)),
        }
        data = self._get("/v5/market/kline", params)
        rows = list(reversed((data.get("result", {}) or {}).get("list") or []))
        out: List[Dict[str, Any]] = []
        for row in rows:
            ts, o, h, l, c, v, t = row
            out.append({
                "ts": int(ts),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v),
                "turnover": float(t),
            })
        return out

    def latest_ohlcv_pack(self, symbol: str, category: str = "spot") -> Dict[str, Any]:
        pack: Dict[str, Any] = {}
        for tf in ("W", "D", "240"):
            try:
                pack[tf] = self.klines(category, symbol, tf, limit=5)
            except Exception as e:
                pack[tf] = {"error": str(e)}
        return pack

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass
