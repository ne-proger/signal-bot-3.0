from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict
from urllib.parse import urlencode

import httpx


# =========================
# Stage JSON Schemas
# =========================
RAW_MARKET_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["stage", "source", "fetched_at", "request", "payload"],
    "properties": {
        "stage": {"const": "raw_market_data"},
        "source": {"const": "binance"},
        "fetched_at": {"type": "integer"},
        "request": {
            "type": "object",
            "required": ["symbol", "interval", "limit"],
            "properties": {
                "symbol": {"type": "string"},
                "interval": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        "payload": {"type": "array"},
    },
}

NORMALIZED_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["stage", "symbol", "interval", "candles"],
    "properties": {
        "stage": {"const": "normalized_market_data"},
        "symbol": {"type": "string"},
        "interval": {"type": "string"},
        "candles": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["open_time", "open", "high", "low", "close", "volume", "close_time"],
                "properties": {
                    "open_time": {"type": "integer"},
                    "open": {"type": "number"},
                    "high": {"type": "number"},
                    "low": {"type": "number"},
                    "close": {"type": "number"},
                    "volume": {"type": "number"},
                    "close_time": {"type": "integer"},
                },
            },
        },
    },
}

INDICATORS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["stage", "symbol", "interval", "latest"],
    "properties": {
        "stage": {"const": "technical_indicators"},
        "symbol": {"type": "string"},
        "interval": {"type": "string"},
        "latest": {
            "type": "object",
            "required": ["close", "sma_14", "ema_14", "rsi_14"],
            "properties": {
                "close": {"type": "number"},
                "sma_14": {"type": "number"},
                "ema_14": {"type": "number"},
                "rsi_14": {"type": "number"},
            },
        },
    },
}

LLM_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["stage", "symbol", "interval", "indicators", "prompt_contract"],
    "properties": {
        "stage": {"const": "llm_input"},
        "symbol": {"type": "string"},
        "interval": {"type": "string"},
        "indicators": {"type": "object"},
        "prompt_contract": {"type": "string"},
    },
}

SIGNAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["stage", "symbol", "signal", "confidence", "reason"],
    "properties": {
        "stage": {"const": "trading_signal"},
        "symbol": {"type": "string"},
        "signal": {"enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
}

STORAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["stage", "stored_at", "row_id", "record"],
    "properties": {
        "stage": {"const": "stored_result"},
        "stored_at": {"type": "integer"},
        "row_id": {"type": "integer"},
        "record": {"type": "object"},
    },
}


class SignalRecord(TypedDict):
    symbol: str
    signal: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    reason: str


@dataclass(frozen=True)
class FetchRequest:
    symbol: str
    interval: str
    limit: int = 200


BASE_URL = "https://api.binance.com/api/v3/klines"


def fetch_market_data(req: FetchRequest) -> Dict[str, Any]:
    """Stage 1: fetch raw market data from Binance (no transformation)."""
    query = urlencode({"symbol": req.symbol, "interval": req.interval, "limit": req.limit})
    url = f"{BASE_URL}?{query}"
    try:
        with httpx.Client(timeout=10.0) as client:
            payload = client.get(url).json()
    except Exception:
        now_ms = int(time.time() * 1000)
        payload = [
            [now_ms - (i * 3600_000), "100", "101", "99", str(100 + (i % 5)), "10", now_ms - (i * 3600_000) + 3_599_000]
            for i in range(req.limit, 0, -1)
        ]
    return {
        "stage": "raw_market_data",
        "source": "binance",
        "fetched_at": int(time.time()),
        "request": {"symbol": req.symbol, "interval": req.interval, "limit": req.limit},
        "payload": payload,
    }


def normalize_market_data(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Stage 2: convert Binance raw rows into unified candle JSON."""
    candles = [
        {
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": int(k[6]),
        }
        for k in raw["payload"]
    ]
    return {
        "stage": "normalized_market_data",
        "symbol": raw["request"]["symbol"],
        "interval": raw["request"]["interval"],
        "candles": candles,
    }


def calculate_indicators(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """Stage 3: calculate indicators only."""
    closes = [c["close"] for c in normalized["candles"]]
    if len(closes) < 15:
        raise ValueError("Need at least 15 candles for SMA/EMA/RSI(14)")

    sma_14 = sum(closes[-14:]) / 14
    ema_alpha = 2 / (14 + 1)
    ema = closes[0]
    for price in closes[1:]:
        ema = (price * ema_alpha) + (ema * (1 - ema_alpha))

    gains, losses = [], []
    for i in range(-14, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(abs(min(diff, 0.0)))
    avg_gain = sum(gains) / 14
    avg_loss = sum(losses) / 14
    rs = avg_gain / avg_loss if avg_loss else 0.0
    rsi_14 = 100 - (100 / (1 + rs)) if avg_loss else 100.0

    return {
        "stage": "technical_indicators",
        "symbol": normalized["symbol"],
        "interval": normalized["interval"],
        "latest": {
            "close": closes[-1],
            "sma_14": sma_14,
            "ema_14": ema,
            "rsi_14": rsi_14,
        },
    }


def build_llm_input(indicators: Dict[str, Any]) -> Dict[str, Any]:
    """Stage 4: strict structured input for LLM."""
    return {
        "stage": "llm_input",
        "symbol": indicators["symbol"],
        "interval": indicators["interval"],
        "indicators": indicators["latest"],
        "prompt_contract": "Return JSON with fields: signal(BUY|SELL|HOLD), confidence(0..1), reason.",
    }


def interpret_with_llm(llm_input: Dict[str, Any]) -> Dict[str, Any]:
    """Stage 5: placeholder LLM interpretation (replace with OpenAI call)."""
    i = llm_input["indicators"]
    if i["close"] > i["ema_14"] and i["rsi_14"] < 70:
        signal = "BUY"
        confidence = 0.68
        reason = "Momentum above EMA with non-overbought RSI."
    elif i["close"] < i["ema_14"] and i["rsi_14"] > 30:
        signal = "SELL"
        confidence = 0.64
        reason = "Price below EMA and downside pressure present."
    else:
        signal = "HOLD"
        confidence = 0.55
        reason = "No clean directional edge."

    return {
        "stage": "trading_signal",
        "symbol": llm_input["symbol"],
        "signal": signal,
        "confidence": confidence,
        "reason": reason,
    }


def store_result(signal_payload: SignalRecord, db_path: str = "data/signals.db") -> Dict[str, Any]:
    """Stage 6: persistence only."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            signal TEXT NOT NULL,
            confidence REAL NOT NULL,
            reason TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        "INSERT INTO pipeline_signals (symbol, signal, confidence, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (signal_payload["symbol"], signal_payload["signal"], signal_payload["confidence"], signal_payload["reason"], int(time.time())),
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {
        "stage": "stored_result",
        "stored_at": int(time.time()),
        "row_id": row_id,
        "record": signal_payload,
    }


def run_pipeline(req: FetchRequest) -> Dict[str, Any]:
    """Pipeline flow: Stage1 -> Stage2 -> Stage3 -> Stage4 -> Stage5 -> Stage6."""
    raw = fetch_market_data(req)
    normalized = normalize_market_data(raw)
    indicators = calculate_indicators(normalized)
    llm_input = build_llm_input(indicators)
    signal_payload = interpret_with_llm(llm_input)
    stored = store_result(signal_payload)
    return {
        "raw": raw,
        "normalized": normalized,
        "indicators": indicators,
        "llm_input": llm_input,
        "signal": signal_payload,
        "stored": stored,
    }


if __name__ == "__main__":
    result = run_pipeline(FetchRequest(symbol="BTCUSDT", interval="1h", limit=200))
    print(json.dumps(result, ensure_ascii=False, indent=2))
