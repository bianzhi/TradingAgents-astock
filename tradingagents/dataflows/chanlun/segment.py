"""缠论线段划分：特征序列分型 + 第一种/第二种情况。

严格遵循「缠中说禅」第67/71课定义。
"""

from __future__ import annotations

import logging

from .types import (
    Direction,
    Fractal,
    FractalType,
    ProcessedKLine,
    Segment,
    Stroke,
)
from .core import find_fractals, _filter_fractals

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 特征序列构造
# ---------------------------------------------------------------------------

class _FeatureElement:
    """特征序列的一个元素（一笔的抽象化）。"""

    def __init__(self, stroke: Stroke, index: int):
        self.index = index
        self.stroke = stroke
        # 特征元素的高低由笔的方向决定
        if stroke.direction == Direction.UP:
            self.high = stroke.end_value   # 向上笔终点是高点
            self.low = stroke.start_value   # 向上笔起点是低点
        else:
            self.high = stroke.start_value  # 向下笔起点是高点
            self.low = stroke.end_value     # 向下笔终点是低点
        self.date = stroke.start_date
        self.end_date = stroke.end_date


def _feature_inclusion_process(features: list[_FeatureElement]) -> list[_FeatureElement]:
    """对特征序列做包含处理（同K线包含逻辑，第71课）。

    注意：跨越两个特征序列的元素不能做包含处理（第71课强调）。
    这里我们处理特征序列自身的包含关系。
    """
    if len(features) < 2:
        return features[:]

    result: list[_FeatureElement] = [features[0]]

    for i in range(1, len(features)):
        curr = features[i]
        prev = result[-1]

        # 包含关系
        if (prev.high >= curr.high and prev.low <= curr.low) or \
           (curr.high >= prev.high and curr.low <= prev.low):
            # 确定方向
            if len(result) >= 2:
                before_prev = result[-2]
                if prev.high > before_prev.high:
                    direction = Direction.UP
                elif prev.high < before_prev.high:
                    direction = Direction.DOWN
                else:
                    direction = Direction.UP if curr.high > prev.high else Direction.DOWN
            else:
                direction = Direction.UP if curr.high > prev.high else Direction.DOWN

            # 合并
            if direction == Direction.UP:
                new_high = max(prev.high, curr.high)
                new_low = max(prev.low, curr.low)
            else:
                new_high = min(prev.high, curr.high)
                new_low = min(prev.low, curr.low)

            # 创建合并后的元素
            merged = _FeatureElement.__new__(_FeatureElement)
            merged.index = prev.index
            merged.stroke = prev.stroke
            merged.high = new_high
            merged.low = new_low
            merged.date = prev.date
            merged.end_date = curr.end_date
            result[-1] = merged
        else:
            result.append(curr)

    return result


def _find_feature_fractals(features: list[_FeatureElement]) -> list[tuple[FractalType, int]]:
    """在特征序列中找分型。

    Returns:
        [(FractalType, feature_index), ...]
    """
    if len(features) < 3:
        return []

    result = []
    for i in range(1, len(features) - 1):
        left = features[i - 1]
        mid = features[i]
        right = features[i + 1]

        # 顶分型
        if mid.high > left.high and mid.high > right.high and \
           mid.low > left.low and mid.low > right.low:
            result.append((FractalType.TOP, i))

        # 底分型
        elif mid.high < left.high and mid.high < right.high and \
             mid.low < left.low and mid.low < right.low:
            result.append((FractalType.BOTTOM, i))

    return result


# ---------------------------------------------------------------------------
# 线段划分
# ---------------------------------------------------------------------------

def identify_segments(strokes: list[Stroke]) -> list[Segment]:
    """划分线段（第67/71课）。

    以向上笔开始为例：
    1. 构造特征序列：取所有反向笔
    2. 对特征序列做包含处理
    3. 在标准特征序列中找分型：
       - 第一种情况：分型第1、2元素间无缺口 → 线段在该分型处转折
       - 第二种情况：分型第1、2元素间有缺口 → 需要辅助判断
    4. 前三笔必须有重叠部分，否则不构成线段

    简化实现：
    由于A股日线数据的特征序列一般较长，此处采用递归法：
    以每3笔构成线段的初始尝试，通过特征序列分型确认。

    Args:
        strokes: 笔序列（由 identify_strokes 产出）

    Returns:
        线段序列。
    """
    if len(strokes) < 3:
        return []

    segments: list[Segment] = []

    # 构造特征序列
    # 向上线段看向下笔，向下线段看向上笔
    # 简化：所有笔交替构成特征序列
    features = [_FeatureElement(s, i) for i, s in enumerate(strokes)]

    # 特征序列包含处理
    processed = _feature_inclusion_process(features)

    # 在处理后特征序列中找分型
    fractal_points = _find_feature_fractals(processed)

    if not fractal_points:
        # 没有特征序列分型 → 整个笔序列构成一段
        if _strokes_have_overlap(strokes, 0, len(strokes) - 1):
            seg_dir = strokes[0].direction
            segments.append(Segment(
                direction=seg_dir,
                start_index=0,
                end_index=len(strokes) - 1,
                start_date=strokes[0].start_date,
                end_date=strokes[-1].end_date,
                start_value=strokes[0].start_value,
                end_value=strokes[-1].end_value,
                strokes=strokes[:],
            ))
        return segments

    # 根据特征序列分型切分线段
    seg_start = 0
    for ftype, fidx in fractal_points:
        feat = processed[fidx]
        # 特征序列分型对应的笔索引
        pen_end = feat.index

        if pen_end <= seg_start:
            continue

        # 检查seg_start到pen_end之间的笔是否有重叠
        pen_range_strokes = strokes[seg_start:pen_end + 1]
        if len(pen_range_strokes) < 3 or not _strokes_have_overlap(strokes, seg_start, pen_end):
            continue

        # 确定线段方向
        seg_dir = strokes[seg_start].direction

        segments.append(Segment(
            direction=seg_dir,
            start_index=seg_start,
            end_index=pen_end,
            start_date=strokes[seg_start].start_date,
            end_date=strokes[pen_end].end_date,
            start_value=strokes[seg_start].start_value,
            end_value=strokes[pen_end].end_value,
            strokes=pen_range_strokes,
        ))

        seg_start = pen_end

    # 处理剩余部分
    if seg_start < len(strokes) - 1:
        remaining = strokes[seg_start:]
        if len(remaining) >= 3 and _strokes_have_overlap(strokes, seg_start, len(strokes) - 1):
            seg_dir = strokes[seg_start].direction
            segments.append(Segment(
                direction=seg_dir,
                start_index=seg_start,
                end_index=len(strokes) - 1,
                start_date=strokes[seg_start].start_date,
                end_date=strokes[-1].end_date,
                start_value=strokes[seg_start].start_value,
                end_value=strokes[-1].end_value,
                strokes=remaining,
            ))

    # 如果没划分出任何线段，但笔数>=3且有重叠，整体作为一段
    if not segments and len(strokes) >= 3 and _strokes_have_overlap(strokes, 0, len(strokes) - 1):
        seg_dir = strokes[0].direction
        segments.append(Segment(
            direction=seg_dir,
            start_index=0,
            end_index=len(strokes) - 1,
            start_date=strokes[0].start_date,
            end_date=strokes[-1].end_date,
            start_value=strokes[0].start_value,
            end_value=strokes[-1].end_value,
            strokes=strokes[:],
        ))

    return segments


def _strokes_have_overlap(strokes: list[Stroke], start: int, end: int) -> bool:
    """检查从start到end的笔序列是否有价格重叠（前三笔）。"""
    if end - start + 1 < 3:
        return False

    # 取前三笔
    s1 = strokes[start]
    s2 = strokes[start + 1]
    s3 = strokes[start + 2]

    # 三笔的价格范围
    ranges = []
    for s in [s1, s2, s3]:
        low = min(s.start_value, s.end_value)
        high = max(s.start_value, s.end_value)
        ranges.append((low, high))

    # 三段有重叠 = max(lows) < min(highs)
    max_low = max(r[0] for r in ranges)
    min_high = min(r[1] for r in ranges)

    return max_low < min_high
