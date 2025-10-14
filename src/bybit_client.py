# src/bybit_client.py
from __future__ import annotations
import os
import time
from typing import Any, Dict, List, Optional

import httpx


class BybitError(Exception):
    pass


class BybitClient:
    """
    Устойчивый клиент Bybit v5 для OHLCV.
    Возвращает pack формата:
      {
        "W":   [ {ts, open, high, low, close, volume}, ... ] | {"error": "..."},
        "D":   [ ... ] | {"error": "..."},
        "240": [ ... ] | {"error": "..."},
      }
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.bybit.com",
        proxy_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.proxy_url = proxy_url or os.getenv("PROXY_URL", None)
        self.timeout_s = float(timeout_s or os.getenv("BYBIT_TIMEOUT_SECONDS", "15"))
        self.retries = int(retries or os.getenv("BYBIT_RETRIES", "2"))

        # httpx Client на весь объект — шарим коннекты
        timeout = httpx.Timeout(connect=10.0, read=self.timeout_s, write=10.0, pool=10.0)
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            proxies=self.proxy_url if self.proxy_url else None,
            headers={"User-Agent": "crypto-signal-bot/3.0"},
        )

    # -------------------- утилиты --------------------

    @staticmethod
    def _norm_symbol(symbol: str) -> str:
        s = symbol.strip().upper().replace("/", "")
        # мини защита от мусора вида "BTCUSDTBUY"
        if s.endswith("USDT") and len(s) >= 6:
            return s
        return s

    @staticmethod
    def _parse_kline_list(raw_list: List[List[str]]) -> List[Dict[str, Any]]:
        """
        В v5 Kline list — это массивы строк:
        [ startTime(ms), open, high, low, close, volume, turnover ]
        """
        out: List[Dict[str, Any]] = []
        for item in raw_list:
            try:
                ts = int(item[0])
                o = float(item[1]); h = float(item[2]); l = float(item[3]); c = float(item[4])
                v = float(item[5]) if len(item) > 5 and item[5] is not None else 0.0
                out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
            except Exception:
                continue
        # Bybit возвращает от нового к старому — приведём к хроно-порядку
        out.sort(key=lambda x: x["ts"])
        return out

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Устойчивый GET с ретраями и бэк-оффом.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                r = self.client.get(path, params=params)
                r.raise_for_status()
                data = r.json()
                # формат v5: {"retCode":0,"retMsg":"OK","result":{...}}
                if data.get("retCode") != 0:
                    raise BybitError(f"retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
                return data["result"]
            except Exception as e:
                last_err = e
                # небольшой экспоненциальный бэк-офф
                time.sleep(0.8 * (attempt + 1))
        # если неудачно
        raise BybitError(str(last_err) if last_err else "unknown error")

    # -------------------- публичное API --------------------

    def kline(
        self,
        *,
        symbol: str,
        interval: str,
        category: str = "spot",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Получить свечи для symbol/interval/category.
        interval: "W" | "D" | "240" (4H) и т.д.
        category: "spot" | "linear" | "inverse"
        """
        norm = self._norm_symbol(symbol)
        params = {
            "category": category,
            "symbol": norm,
            "interval": interval,
            "limit": str(limit),
        }
        res = self._get("/v5/market/kline", params)
        rows = res.get("list") or []
        return self._parse_kline_list(rows)

    def latest_ohlcv_pack(self, symbol: str, category: str = "spot") -> Dict[str, Any]:
        """
        Возвращает свечи сразу по 3 ТФ. Любая ошибка по ТФ конвертируется в {"error": "..."}.
        """
        pack: Dict[str, Any] = {}

        for interval in ("W", "D", "240"):  # weekly, daily, 4h
            try:
                bars = self.kline(symbol=symbol, interval=interval, category=category, limit=200)
                if not bars:
                    pack[interval] = {"error": "no_data"}
                else:
                    pack[interval] = bars
            except Exception as e:
                msg = str(e)
                # нормализуем типичные таймауты/ретраи в короткую метку
                if "timeout" in msg.lower() or "timed out" in msg.lower():
                    pack[interval] = {"error": "timed out"}
                else:
                    pack[interval] = {"error": msg}

        return pack
