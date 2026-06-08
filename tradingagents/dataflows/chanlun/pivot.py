"""缠论中枢识别 + 走势类型划分。

严格遵循「缠中说禅」第17/18课定义。
"""

from __future__ import annotations

import logging
from typing import Optional

from .types import (
    Direction,
    Pivot,
    Segment,
    Stroke,
    Trend,
    TrendType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 笔中枢识别（用笔直接构造中枢，而非线段）
# ---------------------------------------------------------------------------

def find_stroke_pivots(
    strokes: list[Stroke],
    min_strokes: int = 3,
) -> list[Pivot]:
    """识别笔中枢：取连续 N 根笔的价格重叠区间构成中枢。

    笔中枢定义（缠论初学者常用做法）：
    至少3根连续笔的高低点有重叠，即：
    - ZG = max(各笔的低点)  — 中枢上沿
    - ZD = min(各笔的高点)  — 中枢下沿
    - 若 ZG < ZD 则无重叠，前移1笔继续

    Args:
        strokes: 笔序列
        min_strokes: 构成中枢所需最少笔数，默认3

    Returns:
        笔中枢序列。
    """
    if len(strokes) < min_strokes:
        return []

    pivots: list[Pivot] = []
    i = 0

    while i <= len(strokes) - min_strokes:
        window = strokes[i:i + min_strokes]

        # 每笔的价格区间
        stroke_ranges = []
        for s in window:
            low = min(s.start_value, s.end_value)
            high = max(s.start_value, s.end_value)
            stroke_ranges.append((low, high))

        zg = max(r[0] for r in stroke_ranges)  # max(lows)
        zd = min(r[1] for r in stroke_ranges)  # min(highs)
        gg = max(r[1] for r in stroke_ranges)  # max(highs)
        dd = min(r[0] for r in stroke_ranges)  # min(lows)

        if zg >= zd:
            # 无重叠，前移1笔
            i += 1
            continue

        # 有重叠 → 构成中枢
        pivot_end_idx = i + min_strokes - 1
        j = pivot_end_idx + 1

        # 检查后续笔是否延伸
        while j < len(strokes):
            s = strokes[j]
            s_low = min(s.start_value, s.end_value)
            s_high = max(s.start_value, s.end_value)
            if s_low <= zg and s_high >= zd:
                # 延伸中枢
                gg = max(gg, s_high)
                dd = min(dd, s_low)
                pivot_end_idx = j
                j += 1
            else:
                break

        pivots.append(Pivot(
            zg=zg,
            zd=zd,
            gg=gg,
            dd=dd,
            start_date=window[0].start_date,
            end_date=strokes[pivot_end_idx].end_date,
            start_index=i,
            end_index=pivot_end_idx,
            is_extending=(pivot_end_idx == len(strokes) - 1),
        ))

        # 下一轮从中枢结束后的下一笔开始
        i = pivot_end_idx + 1

    return pivots


# ---------------------------------------------------------------------------
# 线段中枢识别
# ---------------------------------------------------------------------------

def find_pivots(
    segments: list[Segment],
    min_segments: int = 3,
) -> list[Pivot]:
    """识别中枢（第17/18课）。

    中枢定义：至少三段连续次级别走势类型的重叠区间。
    - ZG = max(三段lows) — 中枢上沿
    - ZD = min(三段highs) — 中枢下沿
    - 如果 ZG < ZD → 无重叠，不构成中枢
    - GG = max(三段highs) — 中枢最高点
    - DD = min(三段lows) — 中枢最低点

    中枢延伸：后续走势的高点或低点触及 [ZD, ZG] 区间则延续。
    中枢扩张：围绕新中枢的波动与围绕前中枢的波动重叠 → 更大级别中枢
    （扩张暂不实现，在后续版本中处理）。

    Args:
        segments: 线段序列（作为次级别走势类型）
        min_segments: 构成中枢所需最少线段数，默认3

    Returns:
        中枢序列。
    """
    if len(segments) < min_segments:
        return []

    pivots: list[Pivot] = []
    i = 0

    while i <= len(segments) - min_segments:
        # 取连续 min_segments 段尝试构成中枢
        window = segments[i:i + min_segments]

        # 每段的价格范围
        seg_ranges = []
        for seg in window:
            low = min(seg.start_value, seg.end_value)
            high = max(seg.start_value, seg.end_value)
            seg_ranges.append((low, high))

        # 计算中枢区间
        zg = max(r[0] for r in seg_ranges)  # max(lows)
        zd = min(r[1] for r in seg_ranges)  # min(highs)
        gg = max(r[1] for r in seg_ranges)  # max(highs)
        dd = min(r[0] for r in seg_ranges)  # min(lows)

        if zg < zd:
            # 无重叠，不构成中枢，前进1段
            i += 1
            continue

        # 构成中枢！检查后续段是否延伸
        pivot_end_idx = i + min_segments - 1
        j = pivot_end_idx + 1

        while j < len(segments):
            seg = segments[j]
            seg_low = min(seg.start_value, seg.end_value)
            seg_high = max(seg.start_value, seg.end_value)

            # 中枢延伸条件：后续段的高低点与[ZD, ZG]有交集
            if seg_low <= zg and seg_high >= zd:
                # 延伸中枢 — 更新GG/DD
                gg = max(gg, seg_high)
                dd = min(dd, seg_low)
                pivot_end_idx = j
                j += 1
            else:
                break

        pivots.append(Pivot(
            zg=zg,
            zd=zd,
            gg=gg,
            dd=dd,
            start_date=window[0].start_date,
            end_date=segments[pivot_end_idx].end_date,
            start_index=i,
            end_index=pivot_end_idx,
            is_extending=(pivot_end_idx == len(segments) - 1),
        ))

        # 下一轮从中枢结束后的下一段开始
        i = pivot_end_idx + 1

    return pivots


# ---------------------------------------------------------------------------
# 走势类型划分
# ---------------------------------------------------------------------------

def classify_trends(
    segments: list[Segment],
    pivots: list[Pivot],
) -> list[Trend]:
    """划分走势类型（第17/18课）。

    - 盘整：只包含1个该级别中枢
    - 上涨趋势：≥2个中枢，且中枢的ZD依次升高，中枢间无重叠
    - 下跌趋势：≥2个中枢，且中枢的ZG依次降低，中枢间无重叠

    中枢间无重叠 = 后一个中枢的ZD > 前一个中枢的ZG（上涨）
                    或 后一个中枢的ZG < 前一个中枢的ZD（下跌）

    Args:
        segments: 线段序列
        pivots: 中枢序列

    Returns:
        走势类型序列。
    """
    if not pivots:
        # 无中枢：整个走势是未完成的
        if segments:
            trend_dir = segments[0].direction
            return [Trend(
                type=TrendType.CONSOLIDATION,
                direction=trend_dir,
                start_date=segments[0].start_date,
                end_date=segments[-1].end_date,
                start_value=segments[0].start_value,
                end_value=segments[-1].end_value,
            )]
        return []

    if len(pivots) == 1:
        # 单中枢 → 盘整
        p = pivots[0]
        # 盘整方向由中枢前后的走势判断
        overall_dir = _determine_trend_direction(segments, p)
        return [Trend(
            type=TrendType.CONSOLIDATION,
            direction=overall_dir,
            start_date=p.start_date,
            end_date=p.end_date,
            start_value=p.dd if overall_dir == Direction.DOWN else p.gg,
            end_value=p.dd if overall_dir == Direction.UP else p.gg,
            pivots=[p],
        )]

    # 多个中枢 → 尝试组合为趋势
    trends: list[Trend] = []
    trend_start_idx = 0  # 当前走势起始中枢索引

    for i in range(1, len(pivots)):
        prev_p = pivots[trend_start_idx]
        curr_p = pivots[i]

        # 判断是否趋势延续
        is_uptrend = curr_p.zd > prev_p.zg  # 中枢上移
        is_downtrend = curr_p.zg < prev_p.zd  # 中枢下移

        if is_uptrend:
            # 继续向上趋势
            if i == len(pivots) - 1:
                # 最后一个中枢，构建趋势
                trend_pivots = pivots[trend_start_idx:i + 1]
                trends.append(Trend(
                    type=TrendType.UPTREND,
                    direction=Direction.UP,
                    start_date=trend_pivots[0].start_date,
                    end_date=trend_pivots[-1].end_date,
                    start_value=trend_pivots[0].dd,
                    end_value=trend_pivots[-1].gg,
                    pivots=trend_pivots,
                ))
        elif is_downtrend:
            # 继续向下趋势
            if i == len(pivots) - 1:
                trend_pivots = pivots[trend_start_idx:i + 1]
                trends.append(Trend(
                    type=TrendType.DOWNTREND,
                    direction=Direction.DOWN,
                    start_date=trend_pivots[0].start_date,
                    end_date=trend_pivots[-1].end_date,
                    start_value=trend_pivots[0].gg,
                    end_value=trend_pivots[-1].dd,
                    pivots=trend_pivots,
                ))
        else:
            # 中枢重叠 → 前一段趋势/盘整结束，新走势开始
            # 先输出前一段
            if i - trend_start_idx >= 2:
                trend_pivots = pivots[trend_start_idx:i]
                direction = Direction.UP if trend_pivots[-1].zd > trend_pivots[0].zg else Direction.DOWN
                trends.append(Trend(
                    type=TrendType.UPTREND if direction == Direction.UP else TrendType.DOWNTREND,
                    direction=direction,
                    start_date=trend_pivots[0].start_date,
                    end_date=trend_pivots[-1].end_date,
                    start_value=trend_pivots[0].dd if direction == Direction.UP else trend_pivots[0].gg,
                    end_value=trend_pivots[-1].gg if direction == Direction.UP else trend_pivots[-1].dd,
                    pivots=trend_pivots,
                ))
            else:
                # 单中枢 → 盘整
                p = pivots[trend_start_idx]
                overall_dir = _determine_trend_direction(segments, p)
                trends.append(Trend(
                    type=TrendType.CONSOLIDATION,
                    direction=overall_dir,
                    start_date=p.start_date,
                    end_date=p.end_date,
                    start_value=p.dd if overall_dir == Direction.DOWN else p.gg,
                    end_value=p.dd if overall_dir == Direction.UP else p.gg,
                    pivots=[p],
                ))

            trend_start_idx = i

            # 处理最后一个中枢
            if i == len(pivots) - 1:
                p = pivots[i]
                overall_dir = _determine_trend_direction(segments, p)
                trends.append(Trend(
                    type=TrendType.CONSOLIDATION,
                    direction=overall_dir,
                    start_date=p.start_date,
                    end_date=p.end_date,
                    start_value=p.dd if overall_dir == Direction.DOWN else p.gg,
                    end_value=p.dd if overall_dir == Direction.UP else p.gg,
                    pivots=[p],
                ))

    # 处理趋势未匹配到的部分（如只有2个重叠的中枢）
    if not trends and len(pivots) >= 2:
        # 全部中枢有重叠 → 整体盘整
        p0 = pivots[0]
        overall_dir = _determine_trend_direction(segments, p0)
        trends.append(Trend(
            type=TrendType.CONSOLIDATION,
            direction=overall_dir,
            start_date=pivots[0].start_date,
            end_date=pivots[-1].end_date,
            start_value=pivots[0].dd if overall_dir == Direction.DOWN else pivots[0].gg,
            end_value=pivots[-1].dd if overall_dir == Direction.UP else pivots[-1].gg,
            pivots=pivots[:],
        ))

    return trends


def _determine_trend_direction(segments: list[Segment], pivot: Pivot) -> Direction:
    """判断中枢所在走势方向。

    通过中枢起止段的走势判断。
    """
    if pivot.start_index < len(segments) and pivot.end_index < len(segments):
        start_seg = segments[pivot.start_index]
        if start_seg.end_value > start_seg.start_value:
            return Direction.UP
        else:
            return Direction.DOWN
    return Direction.UP
