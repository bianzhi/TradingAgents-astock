"""缠论辅助函数：MACD计算。"""

from __future__ import annotations

from typing import Optional


def calc_ema(values: list[float], period: int) -> list[Optional[float]]:
    """计算EMA（指数移动平均）。

    Returns:
        EMA序列，前period-1个值为None。
    """
    if len(values) < period:
        return [None] * len(values)

    result: list[Optional[float]] = [None] * (period - 1)

    # 第一个EMA值 = SMA
    sma = sum(values[:period]) / period
    result.append(sma)

    multiplier = 2.0 / (period + 1)

    for i in range(period, len(values)):
        ema = (values[i] - result[-1]) * multiplier + result[-1]
        result.append(ema)

    return result


def calc_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """计算MACD指标。

    Args:
        closes: 收盘价序列
        fast: 快线EMA周期
        slow: 慢线EMA周期
        signal: 信号线EMA周期

    Returns:
        (dif, dea, histogram) — DIF线、DEA线、MACD柱
    """
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)

    # DIF = EMA_fast - EMA_slow
    dif: list[Optional[float]] = []
    for f, s in zip(ema_fast, ema_slow):
        if f is not None and s is not None:
            dif.append(f - s)
        else:
            dif.append(None)

    # DEA = EMA(DIF, signal)
    valid_dif = [d for d in dif if d is not None]
    dea_valid = calc_ema(valid_dif, signal)

    # 对齐DEA到原始序列
    dea: list[Optional[float]] = []
    valid_idx = 0
    for d in dif:
        if d is not None:
            if valid_idx < len(dea_valid):
                dea.append(dea_valid[valid_idx])
            else:
                dea.append(None)
            valid_idx += 1
        else:
            dea.append(None)

    # MACD柱 = 2 * (DIF - DEA)
    histogram: list[Optional[float]] = []
    for d, e in zip(dif, dea):
        if d is not None and e is not None:
            histogram.append(2 * (d - e))
        else:
            histogram.append(None)

    return dif, dea, histogram


def macd_area(
    histogram: list[Optional[float]],
    start: int,
    end: int,
) -> float:
    """计算MACD柱面积（取绝对值之和）。

    用于背驰判断中比较两段走势的力度。
    """
    area = 0.0
    for i in range(start, end + 1):
        if i < len(histogram) and histogram[i] is not None:
            area += abs(histogram[i])
    return area
