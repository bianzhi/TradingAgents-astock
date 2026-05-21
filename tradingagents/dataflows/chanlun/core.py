"""缠论核心算法：K线包含处理、分型识别、笔划分。

严格遵循「缠中说禅」第65课定义。
"""

from __future__ import annotations

import logging
from typing import Optional

from .types import (
    Direction,
    Fractal,
    FractalType,
    ProcessedKLine,
    Stroke,
)

logger = logging.getLogger(__name__)


def _get(k, *keys, default=''):
    """从对象或字典中获取值，支持多种键名（属性/小写键/大写键）。"""
    for key in keys:
        if hasattr(k, key):
            val = getattr(k, key)
            return str(val) if key in ('date', 'Date') else val
        if isinstance(k, dict) and key in k:
            val = k[key]
            return str(val) if key in ('date', 'Date') else val
    return default


# ---------------------------------------------------------------------------
# 1. K线包含处理
# ---------------------------------------------------------------------------

def _has_inclusion(k1: ProcessedKLine, k2: ProcessedKLine) -> bool:
    """判断两根K线是否存在包含关系（高低点完全包含）。"""
    return (k1.high >= k2.high and k1.low <= k2.low) or \
           (k2.high >= k1.high and k2.low <= k1.low)


def process_inclusion(klines: list) -> list[ProcessedKLine]:
    """处理K线包含关系（第65课）。

    从左到右遍历，当相邻两根K线存在包含关系时合并：
    - 向上时: 取 max(high), max(low)
    - 向下时: 取 min(high), min(low)

    方向由前一根K线的比较确定。第一根K线无法确定方向时，
    由第二根K线与第一根的高低关系确定。
    若前两根等高等低（极罕见），等第三根来确定方向。

    Args:
        klines: 原始K线列表，每个元素需有 date/open/high/low/close/volume 属性或字段。

    Returns:
        处理后K线列表（无包含关系）。
    """
    if len(klines) < 2:
        result = []
        for i, k in enumerate(klines):
            result.append(ProcessedKLine(
                date=_get(k, 'date', 'Date'),
                open=float(_get(k, 'open', 'Open')),
                high=float(_get(k, 'high', 'High')),
                low=float(_get(k, 'low', 'Low')),
                close=float(_get(k, 'close', 'Close')),
                volume=int(_get(k, 'volume', 'Volume')),
                original_indices=[i],
            ))
        return result

    # 构建初始列表
    raw: list[ProcessedKLine] = []
    for i, k in enumerate(klines):
        raw.append(ProcessedKLine(
            date=_get(k, 'date', 'Date'),
            open=float(_get(k, 'open', 'Open')),
            high=float(_get(k, 'high', 'High')),
            low=float(_get(k, 'low', 'Low')),
            close=float(_get(k, 'close', 'Close')),
            volume=int(_get(k, 'volume', 'Volume')),
            original_indices=[i],
        ))

    result: list[ProcessedKLine] = [raw[0]]

    for i in range(1, len(raw)):
        curr = raw[i]
        prev = result[-1]

        if _has_inclusion(prev, curr):
            # 需要确定方向来决定合并方式
            # 方向由"更早前一根"决定：如果在result中prev不是第一个，看prev的前一根
            if len(result) >= 2:
                before_prev = result[-2]
                # before_prev → prev 的方向
                if prev.high > before_prev.high and prev.low > before_prev.low:
                    direction = Direction.UP
                elif prev.high < before_prev.high and prev.low < before_prev.low:
                    direction = Direction.DOWN
                else:
                    # before_prev和prev也有包含或相等，则通过prev和curr判断
                    if curr.high > prev.high:
                        direction = Direction.UP
                    else:
                        direction = Direction.DOWN
            else:
                # prev是第一根，用prev和curr的高低判断
                if curr.high > prev.high:
                    direction = Direction.UP
                elif curr.low < prev.low:
                    direction = Direction.DOWN
                else:
                    # 完全相等（极罕见），默认向上
                    direction = Direction.UP

            # 合并
            if direction == Direction.UP:
                new_high = max(prev.high, curr.high)
                new_low = max(prev.low, curr.low)
            else:
                new_high = min(prev.high, curr.high)
                new_low = min(prev.low, curr.low)

            merged = ProcessedKLine(
                date=prev.date,  # 保留前一根的时间标记
                open=prev.open,
                high=new_high,
                low=new_low,
                close=curr.close,
                volume=prev.volume + curr.volume,
                original_indices=prev.original_indices + curr.original_indices,
            )
            result[-1] = merged
        else:
            result.append(curr)

    return result


# ---------------------------------------------------------------------------
# 2. 分型识别
# ---------------------------------------------------------------------------

def find_fractals(klines: list[ProcessedKLine]) -> list[Fractal]:
    """识别顶底分型（第65课）。

    三根K线一组：
    - 顶分型: K[i-1] 是三根中高低点最高的
    - 底分型: K[i-1] 是三根中高低点最低的

    即：顶分型 — 左右两根K线的高点和低点都低于中间那根。
         底分型 — 左右两根K线的高点和低点都高于中间那根。
    """
    if len(klines) < 3:
        return []

    fractals: list[Fractal] = []

    for i in range(1, len(klines) - 1):
        left = klines[i - 1]
        mid = klines[i]
        right = klines[i + 1]

        # 顶分型: mid.high > left.high, mid.high > right.high,
        #          mid.low > left.low,   mid.low > right.low
        if mid.high > left.high and mid.high > right.high and \
           mid.low > left.low and mid.low > right.low:
            fractals.append(Fractal(
                type=FractalType.TOP,
                index=i,
                date=mid.date,
                value=mid.high,
            ))

        # 底分型: mid.high < left.high, mid.high < right.high,
        #          mid.low < left.low,   mid.low < right.low
        elif mid.high < left.high and mid.high < right.high and \
             mid.low < left.low and mid.low < right.low:
            fractals.append(Fractal(
                type=FractalType.BOTTOM,
                index=i,
                date=mid.date,
                value=mid.low,
            ))

    return fractals


# ---------------------------------------------------------------------------
# 3. 笔划分
# ---------------------------------------------------------------------------

def _filter_fractals(fractals: list[Fractal]) -> list[Fractal]:
    """过滤分型：连续同类型只保留极值那个。

    规则：
    1. 连续两个顶分型 → 保留高点更高的那个
    2. 连续两个底分型 → 保留低点更低的那个
    3. 顶底必须交替出现
    """
    if not fractals:
        return []

    filtered: list[Fractal] = [fractals[0]]

    for f in fractals[1:]:
        last = filtered[-1]

        if f.type == last.type:
            # 同类型，保留极值
            if f.type == FractalType.TOP:
                if f.value > last.value:
                    filtered[-1] = f
            else:  # BOTTOM
                if f.value < last.value:
                    filtered[-1] = f
        else:
            # 不同类型，加入
            # 但需检查：如果前一个顶比后一个底还低，或前一个底比后一个顶还高，
            # 则不符合笔的定义，需调整
            filtered.append(f)

    # 二次调整：确保顶底的值满足"顶高于底"
    # 如果出现 顶的值 <= 后续底的值，或 底的值 >= 后续顶的值，删除较弱的那一个
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(filtered) - 1:
            curr = filtered[i]
            nxt = filtered[i + 1]

            # 顶后跟底，顶值应 > 底值
            if curr.type == FractalType.TOP and nxt.type == FractalType.BOTTOM:
                if curr.value <= nxt.value:
                    # 删除较弱的那个
                    if i > 0:
                        filtered.pop(i)
                    else:
                        filtered.pop(i + 1)
                    changed = True
                    continue

            # 底后跟顶，底值应 < 顶值
            elif curr.type == FractalType.BOTTOM and nxt.type == FractalType.TOP:
                if curr.value >= nxt.value:
                    if i > 0:
                        filtered.pop(i)
                    else:
                        filtered.pop(i + 1)
                    changed = True
                    continue

            i += 1

    return filtered


def identify_strokes(
    klines: list[ProcessedKLine],
    fractals: list[Fractal] | None = None,
) -> list[Stroke]:
    """划分笔（第65课）。

    规则：
    1. 顶底必须交替
    2. 一笔 = 一个顶分型到一个底分型（或反之）
    3. 顶底之间至少有1根独立K线（处理后K线）
       即从顶分型到相邻底分型之间，在处理后K线中至少间隔1根。

    Args:
        klines: 处理后K线序列
        fractals: 可选，预计算的分型列表。若为None则自动计算。

    Returns:
        笔序列。
    """
    if fractals is None:
        fractals = find_fractals(klines)

    if len(fractals) < 2:
        return []

    # 过滤分型
    filtered = _filter_fractals(fractals)

    if len(filtered) < 2:
        return []

    # 检查顶底之间是否有足够间隔（至少1根独立K线）
    # 此处"独立K线"指顶底分型中间的K线索引差 > 0
    # 分型的index是处理后K线序列的位置
    strokes: list[Stroke] = []

    for i in range(len(filtered) - 1):
        f1 = filtered[i]
        f2 = filtered[i + 1]

        # 顶底交替性检查
        if f1.type == f2.type:
            continue

        # 独立K线检查：两个分型之间至少间隔1根处理后K线
        # 分型index之差 >= 2 意味着中间至少有1根K线（分型中间的那根）
        # 但严格来说，分型本身由3根K线构成，两个相邻分型如果共享K线，
        # 则中间没有独立K线。index差>=3才保证至少1根独立K线。
        # 第65课：顶底分型之间至少有1根独立K线不可共用
        if abs(f2.index - f1.index) < 3:
            # 特殊情况：如果两个分型之间连1根独立K线都没有，
            # 需要取强弱判断保留哪个
            continue

        if f1.type == FractalType.TOP:
            direction = Direction.DOWN
        else:
            direction = Direction.UP

        strokes.append(Stroke(
            direction=direction,
            start_index=f1.index,
            end_index=f2.index,
            start_date=f1.date,
            end_date=f2.date,
            start_value=f1.value,
            end_value=f2.value,
        ))

    return strokes
