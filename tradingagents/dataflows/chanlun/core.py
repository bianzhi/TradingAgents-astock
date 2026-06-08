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
# 3. 笔划分（严格参照 yifangmoyan 五步流程）
#
# 步骤：
#   1. K线去包含（process_inclusion）
#   2. 顶底分型识别 + ensure_alternating
#   3. 初步顶底对匹配（match_fx_pairs）
#   4. 分型失效检查（check_fx_invalidation）→ 重新配对
#   5. 生成笔
# ---------------------------------------------------------------------------

# ═══════════════════════════════════════════════════════════════════════════
# 步骤 2-helper: 强制顶底交替
# ═══════════════════════════════════════════════════════════════════════════

def ensure_alternating(fxs: list[Fractal]) -> list[Fractal]:
    """强制顶底交替 — 连续同类型只保留极值。

    规则：
    - 连续顶分型 → 保留 high 更高的
    - 连续底分型 → 保留 low 更低的
    - 方向正确性（顶 > 底）由后续 match_fx_pairs 处理
    """
    if not fxs:
        return []
    out = [fxs[0]]
    for f in fxs[1:]:
        if f.type == out[-1].type:
            # 同类型：保留极值
            if (f.type == FractalType.TOP and f.value > out[-1].value) or \
               (f.type == FractalType.BOTTOM and f.value < out[-1].value):
                out[-1] = f
        else:
            out.append(f)
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 步骤 3: 初步匹配顶底对
# ═══════════════════════════════════════════════════════════════════════════

def _match_fx_pairs(fxs: list[Fractal]) -> list[tuple[int, int]]:
    """在 fxs 序列上初步匹配顶底对。

    要求：
    - fxs 已通过 ensure_alternating 处理，理论上已交替
    - 分型必须反向（Top→Bottom 或 Bottom→Top）
    - 两个分型的 index 间隔 ≥ 3（中间至少 1 根独立K线）
    - 方向正确：顶 > 底
    - 同类型分型：保留更极端的（在 ensure_alternating 中已处理，
      但匹配过程中仍可能有残留）

    Returns:
        list[tuple[int, int]] — 每对为 (start_idx_in_fxs, end_idx_in_fxs)
    """
    pairs: list[tuple[int, int]] = []
    if len(fxs) < 2:
        return pairs

    curr_pos = 0
    i = 1
    while i < len(fxs):
        curr = fxs[curr_pos]
        nxt = fxs[i]

        # 同类型：保留更极端的，继续前进
        if nxt.type == curr.type:
            should_replace = (curr.type == FractalType.TOP and nxt.value > curr.value) or \
                             (curr.type == FractalType.BOTTOM and nxt.value < curr.value)
            if should_replace:
                curr_pos = i
            i += 1
            continue

        # 间隔检查：分型 index 差 ≥ 3（中间至少 1 根独立K线）
        if abs(nxt.index - curr.index) < 3:
            i += 1
            continue

        # 方向检查：顶 > 底
        direction_ok = (curr.type == FractalType.TOP and nxt.value < curr.value) or \
                       (curr.type == FractalType.BOTTOM and nxt.value > curr.value)
        if direction_ok:
            pairs.append((curr_pos, i))
            curr_pos = i  # 匹配成功，终点作为下一笔起点

        i += 1

    return pairs


# ═══════════════════════════════════════════════════════════════════════════
# 步骤 4: 分型失效检查
# ═══════════════════════════════════════════════════════════════════════════

def _check_fx_invalidation(
    fxs: list[Fractal],
    pairs: list[tuple[int, int]],
    bars: list[ProcessedKLine],
) -> list[tuple[int, int]]:
    """检查分型是否失效，失效则整对删除。

    核心规则（缠论第62、77课）：
    - 检查范围：从 start_fx.index+1 到 end_fx.index-1（即下一个反向分型 k1 前）
    - 一旦反向分型 k1 出现，前分型锁定不可逆
    - 顶分型：该区间内有更高的 high → 失效
    - 底分型：该区间内有更低的 low → 失效
    - 整对失效：start 和 end 分型都移除
    """
    if not pairs:
        return []

    invalid_indices: set[int] = set()

    for pair in pairs:
        si, ei = pair
        start_fx = fxs[si]
        end_fx = fxs[ei]

        # 检查起始位置：分型 index 的下一根 bar
        check_start = start_fx.index + 1
        # 检查终止位置：反向分型 index-1（即其 k1 前一位置）
        check_end = end_fx.index - 1

        if check_start > check_end:
            continue

        invalid = False
        if start_fx.type == FractalType.TOP:
            # 顶分型：区间内有更高点 → 失效
            for j in range(check_start, check_end + 1):
                if j < len(bars) and bars[j].high > start_fx.value:
                    invalid = True
                    break
        else:
            # 底分型：区间内有更低点 → 失效
            for j in range(check_start, check_end + 1):
                if j < len(bars) and bars[j].low < start_fx.value:
                    invalid = True
                    break

        if invalid:
            invalid_indices.add(si)
            invalid_indices.add(ei)

    # 保留有效分型，重新配对
    valid_indices = [i for i in range(len(fxs)) if i not in invalid_indices]
    return _match_fx_pairs_from_indices(fxs, valid_indices)


# ═══════════════════════════════════════════════════════════════════════════
# 步骤 4-helper: 用指定索引子集重新配对
# ═══════════════════════════════════════════════════════════════════════════

def _match_fx_pairs_from_indices(
    fxs: list[Fractal],
    indices: list[int],
) -> list[tuple[int, int]]:
    """用指定的分型子集重新配对。

    与 _match_fx_pairs 不同的是，子集中可能出现连续同类型分型
    （失效删除可能暴露出被交替过滤掉的分型），此时需要回溯修改
    已确认 pair 的端点（替换为更极端的）。
    """
    pairs: list[tuple[int, int]] = []
    if len(indices) < 2:
        return pairs

    curr_idx = 0  # 在 indices 中的位置
    i = 1
    while i < len(indices):
        curr = fxs[indices[curr_idx]]
        nxt = fxs[indices[i]]

        # 同类型：保留更极端的，如已配对则回溯修改
        if nxt.type == curr.type:
            should_replace = (curr.type == FractalType.TOP and nxt.value > curr.value) or \
                             (curr.type == FractalType.BOTTOM and nxt.value < curr.value)
            if should_replace:
                # 回溯：如果已配对且终点就是 curr，更新终点
                if pairs and pairs[-1][1] == indices[curr_idx]:
                    pairs[-1] = (pairs[-1][0], indices[i])
                curr_idx = i
            i += 1
            continue

        # 间隔检查
        if abs(nxt.index - curr.index) < 3:
            i += 1
            continue

        # 方向检查
        direction_ok = (curr.type == FractalType.TOP and nxt.value < curr.value) or \
                       (curr.type == FractalType.BOTTOM and nxt.value > curr.value)
        if direction_ok:
            pairs.append((indices[curr_idx], indices[i]))
            curr_idx = i

        i += 1

    return pairs


# ═══════════════════════════════════════════════════════════════════════════
# 步骤 5: 生成笔
# ═══════════════════════════════════════════════════════════════════════════

def _create_strokes(
    fxs: list[Fractal],
    pairs: list[tuple[int, int]],
) -> list[Stroke]:
    """将顶底对转为笔。"""
    strokes: list[Stroke] = []
    for si, ei in pairs:
        start_fx = fxs[si]
        end_fx = fxs[ei]

        direction = Direction.DOWN if start_fx.type == FractalType.TOP else Direction.UP
        strokes.append(Stroke(
            direction=direction,
            start_index=start_fx.index,
            end_index=end_fx.index,
            start_date=start_fx.date,
            end_date=end_fx.date,
            start_value=start_fx.value,
            end_value=end_fx.value,
        ))
    return strokes


# ═══════════════════════════════════════════════════════════════════════════
# 入口函数
# ═══════════════════════════════════════════════════════════════════════════

def identify_strokes(
    klines: list[ProcessedKLine],
    fractals: list[Fractal] | None = None,
) -> list[Stroke]:
    """划分笔 — 严格五步流程。

    1. （klines 已通过 process_inclusion 去包含）
    2. 分型识别 + ensure_alternating
    3. 初步匹配顶底对
    4. 分型失效检查 → 重新配对
    5. 生成笔

    Args:
        klines: 处理后K线序列（已去包含）
        fractals: 可选，预计算的分型列表。若为None则自动计算。

    Returns:
        笔序列（严格交替）。
    """
    if fractals is None:
        fractals = find_fractals(klines)

    if len(fractals) < 2:
        return []

    # 步骤2b: 强制交替
    fxs = ensure_alternating(fractals)
    if len(fxs) < 2:
        return []

    # 步骤3: 初步匹配
    pairs = _match_fx_pairs(fxs)
    if not pairs:
        return []

    # 步骤4: 失效检查 + 重新配对
    valid_pairs = _check_fx_invalidation(fxs, pairs, klines)

    # 步骤5: 生成笔
    return _create_strokes(fxs, valid_pairs)
