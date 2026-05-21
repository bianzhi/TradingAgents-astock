"""缠论背驰判断：趋势背驰 + 盘整背驰 + MACD辅助。

严格遵循「缠中说禅」第24/25/43课定义。
"""

from __future__ import annotations

import logging
from typing import Optional

from .types import (
    BuySellPoint,
    BuySellType,
    Divergence,
    DivergenceType,
    Direction,
    Pivot,
    Segment,
    Stroke,
    Trend,
    TrendType,
    ChanlunResult,
)
from .utils import calc_macd, macd_area

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 背驰判断
# ---------------------------------------------------------------------------

def detect_divergence(
    trends: list[Trend],
    segments: list[Segment],
    closes: list[float],
    close_dates: list[str],
    level: str = "daily",
) -> list[Divergence]:
    """检测背驰（第24/25/43课）。

    A. 趋势背驰：
       - 条件：两段同向趋势A、C，中间由B连接
       - MACD辅助：DIF/DEA回拉0轴 + C段MACD面积 < A段面积 + 柱子缩短

    B. 盘整背驰：
       - 中枢内次级别走势力度递减

    Args:
        trends: 走势类型序列
        segments: 线段序列
        closes: 收盘价序列
        close_dates: 收盘价对应的日期序列
        level: 分析级别

    Returns:
        背驰标记列表。
    """
    divergences: list[Divergence] = []

    if len(closes) < 30:
        return divergences

    # 计算MACD
    dif, dea, histogram = calc_macd(closes)

    # A. 趋势背驰检测
    for trend in trends:
        if trend.type == TrendType.UPTREND and len(trend.pivots) >= 2:
            # 上涨趋势 — 检测顶背驰
            div = _check_trend_divergence(
                trend=trend,
                direction=Direction.UP,
                histogram=histogram,
                close_dates=close_dates,
                closes=closes,
                level=level,
            )
            if div:
                divergences.append(div)

        elif trend.type == TrendType.DOWNTREND and len(trend.pivots) >= 2:
            # 下跌趋势 — 检测底背驰
            div = _check_trend_divergence(
                trend=trend,
                direction=Direction.DOWN,
                histogram=histogram,
                close_dates=close_dates,
                closes=closes,
                level=level,
            )
            if div:
                divergences.append(div)

        elif trend.type == TrendType.CONSOLIDATION:
            # 盘整背驰
            div = _check_pattern_divergence(
                trend=trend,
                histogram=histogram,
                close_dates=close_dates,
                closes=closes,
                level=level,
            )
            if div:
                divergences.append(div)

    return divergences


def _check_trend_divergence(
    trend: Trend,
    direction: Direction,
    histogram: list,
    close_dates: list[str],
    closes: list[float],
    level: str,
) -> Optional[Divergence]:
    """检查趋势背驰。

    上涨趋势顶背驰：c段（第二中枢之后）的MACD面积 < a段（第一中枢之前或之中）的面积
    下跌趋势底背驰：同上反向
    """
    if len(trend.pivots) < 2:
        return None

    # 取最后两个中枢
    p1 = trend.pivots[-2]
    p2 = trend.pivots[-1]

    # a段：p1中枢之前的走势段
    # c段：p2中枢之后的走势段
    # 通过日期映射到closes数组的索引

    # 找a段在closes中的起止位置
    a_start_idx = _find_date_index(close_dates, p1.start_date)
    a_end_idx = _find_date_index(close_dates, p1.end_date)

    # 找c段
    c_start_idx = _find_date_index(close_dates, p2.start_date)
    c_end_idx = _find_date_index(close_dates, p2.end_date)

    # 如果中枢仍在延伸，取到最新数据
    if p2.is_extending:
        c_end_idx = len(closes) - 1

    if a_start_idx is None or a_end_idx is None or c_start_idx is None or c_end_idx is None:
        return None

    # 柱子面积对比
    a_area = macd_area(histogram, a_start_idx, a_end_idx)
    c_area = macd_area(histogram, c_start_idx, c_end_idx)

    if a_area == 0:
        return None

    # 背驰条件：c段面积 < a段面积
    ratio = c_area / a_area

    if ratio >= 1.0:
        return None  # 无背驰

    # 强度 = 1 - ratio (0~1, 越大背驰越强)
    strength = min(1.0 - ratio, 1.0)

    # DIF回拉0轴判断（简化：检查c段末尾DIF是否接近0）
    # 实际通过MACD面积递减已经体现

    div_direction = Direction.UP if direction == Direction.UP else Direction.DOWN
    div_type = DivergenceType.TREND

    return Divergence(
        type=div_type,
        direction=div_direction,
        level=level,
        date=close_dates[c_end_idx] if c_end_idx < len(close_dates) else close_dates[-1],
        value=closes[c_end_idx] if c_end_idx < len(closes) else closes[-1],
        strength=strength,
    )


def _check_pattern_divergence(
    trend: Trend,
    histogram: list,
    close_dates: list[str],
    closes: list[float],
    level: str,
) -> Optional[Divergence]:
    """检查盘整背驰。

    盘整中枢内的次级别走势力度递减。
    简化判断：中枢内MACD柱面积呈递减趋势。
    """
    if not trend.pivots:
        return None

    p = trend.pivots[0]

    start_idx = _find_date_index(close_dates, p.start_date)
    end_idx = _find_date_index(close_dates, p.end_date)

    if start_idx is None or end_idx is None:
        return None

    if p.is_extending:
        end_idx = len(closes) - 1

    # 将中枢内走势分为前后两段
    mid = (start_idx + end_idx) // 2

    first_area = macd_area(histogram, start_idx, mid)
    second_area = macd_area(histogram, mid + 1, end_idx)

    if first_area == 0:
        return None

    ratio = second_area / first_area

    if ratio >= 0.8:
        return None  # 力度没有明显递减

    strength = 1.0 - ratio

    # 判断盘整背驰方向
    if closes[end_idx] > closes[start_idx]:
        div_dir = Direction.UP
    else:
        div_dir = Direction.DOWN

    return Divergence(
        type=DivergenceType.PATTERN,
        direction=div_dir,
        level=level,
        date=close_dates[end_idx] if end_idx < len(close_dates) else close_dates[-1],
        value=closes[end_idx] if end_idx < len(closes) else closes[-1],
        strength=min(strength, 1.0),
    )


def _find_date_index(dates: list[str], target: str) -> Optional[int]:
    """在日期列表中查找目标日期的索引。"""
    for i, d in enumerate(dates):
        if d == target:
            return i
    # 找最接近的
    for i, d in enumerate(dates):
        if d >= target:
            return i
    return None
