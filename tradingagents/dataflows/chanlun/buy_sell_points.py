"""缠论三类买卖点识别。

严格遵循「缠中说禅」第20/21课定义。

买点：
  - 一买：下跌趋势底背驰点 — 趋势必然转折
  - 二买：一买后次级别回抽低点
  - 三买：次级别离开中枢后回试不破ZG

卖点：
  - 一卖：上涨趋势顶背驰点
  - 二卖：一卖后次级别回抽高点
  - 三卖：次级别离开中枢后回试不破ZD
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
)

logger = logging.getLogger(__name__)


def find_buy_sell_points(
    divergences: list[Divergence],
    trends: list[Trend],
    pivots: list[Pivot],
    segments: list[Segment],
    closes: list[float],
    close_dates: list[str],
    level: str = "daily",
) -> list[BuySellPoint]:
    """识别三类买卖点。

    Args:
        divergences: 背驰标记列表
        trends: 走势类型列表
        pivots: 中枢列表
        segments: 线段列表
        closes: 收盘价序列
        close_dates: 日期序列
        level: 分析级别

    Returns:
        买卖点列表。
    """
    points: list[BuySellPoint] = []

    # ---- 一类买卖点：趋势背驰点 ----
    for div in divergences:
        if div.type != DivergenceType.TREND:
            continue

        if div.direction == Direction.DOWN and _is_downtrend_divergence(div, trends):
            # 下跌趋势底背驰 → 一买
            points.append(BuySellPoint(
                type=BuySellType.FIRST_BUY,
                date=div.date,
                value=div.value,
                level=level,
                confidence=div.strength,
            ))

        elif div.direction == Direction.UP and _is_uptrend_divergence(div, trends):
            # 上涨趋势顶背驰 → 一卖
            points.append(BuySellPoint(
                type=BuySellType.FIRST_SELL,
                date=div.date,
                value=div.value,
                level=level,
                confidence=div.strength,
            ))

    # ---- 三类买卖点：中枢回试 ----
    for pivot in pivots:
        pt = _check_third_point(pivot, segments, closes, close_dates, level)
        if pt:
            points.append(pt)

    # ---- 二类买卖点：一类点后的回抽 ----
    second_points = _find_second_points(points, segments, closes, close_dates, level)
    points.extend(second_points)

    # 按日期排序
    points.sort(key=lambda p: p.date)

    return points


def _is_downtrend_divergence(div: Divergence, trends: list[Trend]) -> bool:
    """检查背驰是否发生在下跌趋势末尾。"""
    for trend in trends:
        if trend.type == TrendType.DOWNTREND and trend.end_date >= div.date:
            if trend.start_date <= div.date:
                return True
    return False


def _is_uptrend_divergence(div: Divergence, trends: list[Trend]) -> bool:
    """检查背驰是否发生在上涨趋势末尾。"""
    for trend in trends:
        if trend.type == TrendType.UPTREND and trend.end_date >= div.date:
            if trend.start_date <= div.date:
                return True
    return False


def _check_third_point(
    pivot: Pivot,
    segments: list[Segment],
    closes: list[float],
    close_dates: list[str],
    level: str,
) -> Optional[BuySellPoint]:
    """检查中枢的第三类买卖点。

    三买：次级别离开中枢后回试不破ZG
    三卖：次级别离开中枢后回试不破ZD
    """
    if pivot.end_index >= len(segments):
        return None

    # 中枢之后的段
    post_segments = segments[pivot.end_index + 1:]

    if not post_segments:
        return None

    # 找出中枢后的离开和回试
    # 简化：中枢之后第一段向下回试看是否破ZG（三买）
    # 中枢之后第一段向上回试看是否破ZD（三卖）

    for seg in post_segments[:3]:  # 只看最近3段
        seg_low = min(seg.start_value, seg.end_value)
        seg_high = max(seg.start_value, seg.end_value)

        # 向下回试不破ZG → 三买
        if seg.end_value < seg.start_value:  # 向下段
            if seg_low > pivot.zg:
                return BuySellPoint(
                    type=BuySellType.THIRD_BUY,
                    date=seg.end_date,
                    value=seg.end_value,
                    level=level,
                    confidence=0.6,
                    related_pivot=pivot,
                )

        # 向上回试不破ZD → 三卖
        elif seg.end_value > seg.start_value:  # 向上段
            if seg_high < pivot.zd:
                return BuySellPoint(
                    type=BuySellType.THIRD_SELL,
                    date=seg.end_date,
                    value=seg.end_value,
                    level=level,
                    confidence=0.6,
                    related_pivot=pivot,
                )

    return None


def _find_second_points(
    first_points: list[BuySellPoint],
    segments: list[Segment],
    closes: list[float],
    close_dates: list[str],
    level: str,
) -> list[BuySellPoint]:
    """寻找二类买卖点。

    二买：一买之后，次级别回抽的低点（高于一买价格）
    二卖：一卖之后，次级别回抽的高点（低于一卖价格）
    """
    second_points: list[BuySellPoint] = []

    for fp in first_points:
        if fp.type == BuySellType.FIRST_BUY:
            # 找一买后的回抽低点
            idx = _find_date_index(close_dates, fp.date)
            if idx is None:
                continue

            # 在一买之后的段中找回抽
            for seg in segments:
                if seg.start_date <= fp.date:
                    continue
                if seg.end_value < seg.start_value:  # 向下回抽
                    if seg.end_value > fp.value:  # 低点高于一买
                        second_points.append(BuySellPoint(
                            type=BuySellType.SECOND_BUY,
                            date=seg.end_date,
                            value=seg.end_value,
                            level=level,
                            confidence=0.5,
                        ))
                        break  # 只取第一个

        elif fp.type == BuySellType.FIRST_SELL:
            # 找一卖后的回抽高点
            idx = _find_date_index(close_dates, fp.date)
            if idx is None:
                continue

            for seg in segments:
                if seg.start_date <= fp.date:
                    continue
                if seg.end_value > seg.start_value:  # 向上回抽
                    if seg.end_value < fp.value:  # 高点低于一卖
                        second_points.append(BuySellPoint(
                            type=BuySellType.SECOND_SELL,
                            date=seg.end_date,
                            value=seg.end_value,
                            level=level,
                            confidence=0.5,
                        ))
                        break

    return second_points


def _find_date_index(dates: list[str], target: str) -> Optional[int]:
    """在日期列表中查找目标日期的索引。"""
    for i, d in enumerate(dates):
        if d == target:
            return i
    for i, d in enumerate(dates):
        if d >= target:
            return i
    return None
