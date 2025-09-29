from __future__ import annotations
import re

MULT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DEFAULT_SECONDS = 3600

class ParseError(ValueError):
    pass

def parse_frequency(s: str) -> int:
    s = s.strip().lower()
    if s.isdigit():
        val = int(s)
        return max(60, val)
    m = re.fullmatch(r"(\d+)([smhd])", s)
    if not m:
        raise ParseError("Ожидается формат: <N>s|m|h|d, напр. 5m или 1h")
    n, unit = int(m.group(1)), m.group(2)
    seconds = n * MULT[unit]
    seconds = max(60, min(seconds, 31 * 86400))
    return seconds

def norm_pairs(text: str) -> str:
    pairs = [p.strip().upper().replace("/", "") for p in text.split(",") if p.strip()]
    uniq = []
    for p in pairs:
        if p and p not in uniq:
            uniq.append(p)
    return ",".join(uniq) if uniq else "BTCUSDT"

def validate_sensitivity(val: str) -> str:
    v = val.strip().lower()
    if v not in {"low", "medium", "high"}:
        raise ParseError("Чувствительность должна быть: low|medium|high")
    return v

def validate_category(val: string) -> str:  # type: ignore[name-defined]
    v = str(val).strip().lower()
    if v not in {"spot", "linear"}:
        raise ParseError("Категория должна быть: spot|linear")
    return v

# Порог публикации в канал по чувствительности
CONF_THRESHOLDS = {
    "low": 0.80,     # фиксируем только сильные сигналы
    "medium": 0.60,  # сбалансировано
    "high": 0.40,    # даже слабые сигналы
}
