# src/analysis.py
from __future__ import annotations
from typing import Any, Dict, List, Optional
import os, json, time
import pandas as pd
import numpy as np
from pydantic import BaseModel, Field, ValidationError, field_validator

# OpenAI SDK v1.x (как в requirements: openai>=1.40)
try:
    from openai import OpenAI
except Exception as e:
    OpenAI = None  # type: ignore


# -----------------------------
# Модель валидируемого ответа
# -----------------------------
class SignalModel(BaseModel):
    buy_signal: bool
    rationale: str
    entry: Optional[float] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    exit_horizon: Optional[str] = None
    confidence: Optional[float] = None

    @field_validator("confidence")
    @classmethod
    def _conf_range(cls, v, info):
        # если buy_signal=true, confidence обязателен и 0..1
        return v

    @field_validator("entry", "take_profit", "stop_loss", "exit_horizon")
    @classmethod
    def _required_if_buy(cls, v, info):
        # Проверку на обязательность сделаем ниже вручную — так короче
        return v

    def enforce_required_when_buy(self) -> None:
        if self.buy_signal:
            missing = []
            if self.entry is None: missing.append("entry")
            if self.take_profit is None: missing.append("take_profit")
            if self.stop_loss is None: missing.append("stop_loss")
            if self.exit_horizon is None: missing.append("exit_horizon")
            if self.confidence is None or not (0.0 <= float(self.confidence) <= 1.0):
                missing.append("confidence(0..1)")
            if missing:
                raise ValidationError([f"Missing/invalid when buy_signal=true: {', '.join(missing)}"], SignalModel)


# -----------------------------
# Индикаторы
# -----------------------------
def _ma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=max(1, window//2)).mean()

def _macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return pd.DataFrame({"macd": macd, "signal": sig, "hist": hist})


def _tf_summary(bars: List[Dict[str, Any]], ma_window: int, macd_cfg: Dict[str, int]) -> Dict[str, Any]:
    if not bars:
        return {"error": "no_data"}

    df = pd.DataFrame(bars)
    # безопасность типов
    for col in ("open","high","low","close","volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")

    close = df["close"].astype(float)
    ma_last = float(_ma(close, ma_window).iloc[-1])
    macd_df = _macd(close, **macd_cfg).iloc[-1].to_dict()
    macd_last = {k: float(v) for k, v in macd_df.items()}

    vol_annual = float(df["close"].pct_change().std() * np.sqrt(365))
    levels = {
        "high": float(df["high"].max()),
        "low":  float(df["low"].min()),
        "recent_high": float(df["high"].tail(min(10, len(df))).max()),
        "recent_low":  float(df["low"].tail(min(10, len(df))).min()),
    }
    return {
        "last_ts": int(df["ts"].iloc[-1].timestamp() * 1000),
        "close": float(close.iloc[-1]),
        "ma": ma_last,
        "macd": macd_last,
        "volatility": vol_annual,
        "levels": levels,
    }


# -----------------------------
# Подготовка промта
# -----------------------------
def _load_secret_prompt() -> str:
    # Не коммитим этот файл, читаем локально
    try:
        with open(os.path.join("secrets", "market_prompt.txt"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        # минимальный безопасный fallback (без раскрытия методологии)
        return (
            "{{now_utc}} UTC. Проанализируй {{symbol}} по трём ТФ: W, D, 4H. "
            "Ниже будет JSON с OHLCV-резюме и индикаторами. Верни строго JSON по схеме."
        )

def _build_llm_prompt(symbol: str,
                      summaries: Dict[str, Any],
                      params: Dict[str, Any],
                      book_url: Optional[str]) -> str:
    base = _load_secret_prompt()
    body = {
        "now_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "symbol": symbol,
        "params": params,
        "summaries": summaries,
        "book_url": book_url,  # только контекст для модели, печатать ссылку запрещено
        "PROMPT_JSON_SCHEMA": {
            "buy_signal": "bool",
            "rationale": "string",
            "entry": "number (required if buy_signal=true)",
            "take_profit": "number (required if buy_signal=true)",
            "stop_loss": "number (required if buy_signal=true)",
            "exit_horizon": "string (required if buy_signal=true)",
            "confidence": "number 0..1 (required if buy_signal=true)",
        },
    }
    instructions = (
        "\n\nINSTRUCTIONS:\n"
        "- Return STRICT JSON only, no markdown, no comments, no text around.\n"
        "- Use numbers (not strings) for numeric fields.\n"
        "- Do NOT include book_url in the output.\n"
    )
    return base + "\n\nCONTEXT_JSON:\n" + json.dumps(body, ensure_ascii=False) + instructions


# -----------------------------
# Вызов OpenAI (json-only)
# -----------------------------
def _ask_openai_json(prompt: str, model: str, temperature: float = 0.0) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or OpenAI is None:
        raise RuntimeError("OPENAI_API_KEY не задан или OpenAI SDK недоступен")

    client = OpenAI(api_key=api_key)

    # Используем строгий JSON-режим
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You output ONLY JSON that matches the user's schema."},
            {"role": "user", "content": prompt},
        ],
    )
    text = (resp.choices[0].message.content or "").strip()
    data = json.loads(text)

    # Валидация и дополнительные правила
    obj = SignalModel(**data)
    obj.enforce_required_when_buy()
    return obj.model_dump()


# -----------------------------
# Публичная функция анализа
# -----------------------------
def analyze_and_decide(
    symbol: str,
    ohlcv_pack: Dict[str, Any],
    ma_window: int = 21,
    macd_cfg: Optional[Dict[str, int]] = None,
    sensitivity: str = "medium",
    model: str = "gpt-4o-mini-2024-08-06",
    book_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Возвращает dict, соответствующий SignalModel.
    """
    macd_cfg = macd_cfg or {"fast": 12, "slow": 26, "signal": 9}

    # Если по какому-либо ТФ ошибка — сразу короткий отказ (чтобы не жечь токены)
    tf_errors = {tf: v.get("error") for tf, v in ohlcv_pack.items()
                 if isinstance(v, dict) and v.get("error")}
    if tf_errors:
        return {
            "buy_signal": False,
            "rationale": f"Недостаточно данных от Bybit: {tf_errors}. Анализ пропущен.",
            "entry": None, "take_profit": None, "stop_loss": None,
            "exit_horizon": None, "confidence": None,
        }

    # Готовим сводки по TF
    summaries: Dict[str, Any] = {}
    tf_map = {"W": "weekly", "D": "daily", "240": "h4"}
    for tf, name in tf_map.items():
        bars = ohlcv_pack.get(tf) or []
        summaries[name] = _tf_summary(bars, ma_window, macd_cfg)

    params = {
        "ma_window": ma_window,
        "macd": macd_cfg,
        "sensitivity": sensitivity,
    }

    # Если ключа OpenAI нет — безопасный локальный fallback (NO SIGNAL),
    # чтобы пайплайн не падал на локальных тестах
    if not os.getenv("OPENAI_API_KEY") or OpenAI is None:
        last_4h = summaries["h4"].get("close") if isinstance(summaries.get("h4"), dict) else None
        return {
            "buy_signal": False,
            "rationale": f"LOCAL MODE: no OPENAI_API_KEY. Close(4H)={last_4h}, MA/MACD рассчитаны, LLM не вызывался.",
            "entry": None, "take_profit": None, "stop_loss": None,
            "exit_horizon": None, "confidence": None,
        }

    prompt = _build_llm_prompt(symbol, summaries, params, book_url)
    try:
        result = _ask_openai_json(prompt, model=model, temperature=0.0)
        return result
    except Exception as e:
        # Любую ошибку LLM превращаем в безопасный отказ
        return {
            "buy_signal": False,
            "rationale": f"LLM error: {e}",
            "entry": None, "take_profit": None, "stop_loss": None,
            "exit_horizon": None, "confidence": None,
        }
