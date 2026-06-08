"""缠论线段划分：状态机算法（移植自 yifangmoyan/Rust 参考实现）。

严格遵循「缠中说禅」第67/71课定义：
- 特征序列由反向笔构成（向上线段取向下笔，向下线段取向上笔）
- 特征序列包含处理（方向感知：向上段取低低，向下段取高高）
- 一类破坏（无缺口→直接终结）vs 二类破坏（有缺口→需二次确认）
- 前三笔必须有重叠区域
"""

from __future__ import annotations

import logging
from copy import copy

from .types import (
    Direction,
    Segment,
    Stroke,
)

logger = logging.getLogger(__name__)

DEFAULT_MIN_XD_LEN = 3  # 线段最少需要的笔数


# ═══════════════════════════════════════════════════════════════════
#  FeatElem — 特征序列元素（仅 high/low，不含多余信息）
# ═══════════════════════════════════════════════════════════════════

class _FeatElem:
    """特征序列的一个元素。与 Rust 版 FeatElem 一致，只取笔的高低极值。"""

    __slots__ = ("high", "low")

    def __init__(self, high: float, low: float):
        self.high = high
        self.low = low

    @classmethod
    def from_stroke(cls, s: Stroke) -> "_FeatElem":
        return cls(
            high=max(s.start_value, s.end_value),
            low=min(s.start_value, s.end_value),
        )

    def clone(self) -> "_FeatElem":
        return _FeatElem(self.high, self.low)


# ═══════════════════════════════════════════════════════════════════
#  InnerSeg — 线段构建过程中的内部表示
# ═══════════════════════════════════════════════════════════════════

class _InnerSeg:
    """线段构建过程中的内部表示（对齐 Rust InnerSeg）。"""

    __slots__ = ("is_up", "bi_indices", "feature", "confirmed_end")

    def __init__(self, start_bi_idx: int, strokes: list[Stroke]):
        bi = strokes[start_bi_idx]
        self.is_up = bi.direction == Direction.UP
        self.bi_indices: list[int] = [start_bi_idx]
        self.feature: list[_FeatElem] = []  # 反向笔的特征序列（未包含处理）
        self.confirmed_end: bool = False

    def add_bi(self, bi_idx: int, strokes: list[Stroke]):
        """添加一笔到线段，同时收集反向笔进特征序列。"""
        bi = strokes[bi_idx]
        bi_is_up = bi.direction == Direction.UP
        self.bi_indices.append(bi_idx)

        # 向上线段收集向下笔，向下线段收集向上笔
        if self.is_up and not bi_is_up:
            self.feature.append(_FeatElem.from_stroke(bi))
        elif not self.is_up and bi_is_up:
            self.feature.append(_FeatElem.from_stroke(bi))

    def bi_count(self) -> int:
        return len(self.bi_indices)


# ═══════════════════════════════════════════════════════════════════
#  特征序列包含处理（方向感知）
# ═══════════════════════════════════════════════════════════════════

def _merge_feature_include(feat: list[_FeatElem], seg_is_up: bool) -> list[_FeatElem]:
    """特征序列包含处理。

    - 向上线段（特征序列=向下笔）：合并取低低 → high=min, low=min
    - 向下线段（特征序列=向上笔）：合并取高高 → high=max, low=max
    """
    if len(feat) < 2:
        return feat[:]

    res: list[_FeatElem] = [feat[0].clone()]
    for item in feat[1:]:
        last = res[-1]
        # 判断互相包含
        in1 = last.low <= item.low and last.high >= item.high
        in2 = item.low <= last.low and item.high >= last.high
        if not in1 and not in2:
            res.append(item.clone())
            continue
        # 按线段方向合并
        if seg_is_up:
            # 向上线段：合并取低低
            nh = min(last.high, item.high)
            nl = min(last.low, item.low)
        else:
            # 向下线段：合并取高高
            nh = max(last.high, item.high)
            nl = max(last.low, item.low)
        res.pop()
        res.append(_FeatElem(nh, nl))
    return res


# ═══════════════════════════════════════════════════════════════════
#  特征序列分型判断
# ═══════════════════════════════════════════════════════════════════

def _is_top_fx(arr: list[_FeatElem]) -> bool:
    """判断是否出现顶分型（任一中间元素 high 最高）。"""
    if len(arr) < 3:
        return False
    for i in range(1, len(arr) - 1):
        if arr[i].high > arr[i - 1].high and arr[i].high > arr[i + 1].high:
            return True
    return False


def _is_bottom_fx(arr: list[_FeatElem]) -> bool:
    """判断是否出现底分型（任一中间元素 low 最低）。"""
    if len(arr) < 3:
        return False
    for i in range(1, len(arr) - 1):
        if arr[i].low < arr[i - 1].low and arr[i].low < arr[i + 1].low:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════
#  特征序列缺口判断
# ═══════════════════════════════════════════════════════════════════

def _feature_has_gap(feat: list[_FeatElem], seg_is_up: bool) -> bool:
    """判断特征序列分型第一、二元素是否存在缺口。

    只取最后两根（对应分型的前两根）：
    - 向上线段（特征序列=向下笔）：第二根高点 < 第一根低点 = 存在缺口
    - 向下线段（特征序列=向上笔）：第二根低点 > 第一根高点 = 存在缺口
    """
    if len(feat) < 2:
        return False
    f1 = feat[-2]
    f2 = feat[-1]
    if seg_is_up:
        return f2.high < f1.low
    else:
        return f2.low > f1.high


# ═══════════════════════════════════════════════════════════════════
#  前三笔重叠检查
# ═══════════════════════════════════════════════════════════════════

def _check_overlap_of_first_3(strokes: list[Stroke], indices: list[int]) -> bool:
    """缠论：一个线段的前三笔必须有重叠区域。"""
    if len(indices) < 3:
        return False
    max_low = float("-inf")
    min_high = float("inf")
    for idx in indices[:3]:
        s = strokes[idx]
        lo = min(s.start_value, s.end_value)
        hi = max(s.start_value, s.end_value)
        max_low = max(max_low, lo)
        min_high = min(min_high, hi)
    return max_low < min_high


# ═══════════════════════════════════════════════════════════════════
#  内部线段 → 输出格式
# ═══════════════════════════════════════════════════════════════════

def _segment_to_output(seg: _InnerSeg, strokes: list[Stroke]) -> Segment:
    """将内部线段转换为 Segment 输出格式。"""
    first_bi = strokes[seg.bi_indices[0]]
    last_bi = strokes[seg.bi_indices[-1]]
    xd_dir = Direction.UP if seg.is_up else Direction.DOWN
    return Segment(
        direction=xd_dir,
        start_index=seg.bi_indices[0],
        end_index=seg.bi_indices[-1],
        start_date=first_bi.start_date,
        end_date=last_bi.end_date,
        start_value=first_bi.start_value,
        end_value=last_bi.end_value,
        strokes=[strokes[i] for i in seg.bi_indices],
    )


# ═══════════════════════════════════════════════════════════════════
#  主入口：identify_segments
# ═══════════════════════════════════════════════════════════════════

def identify_segments(
    strokes: list[Stroke],
    min_len: int = DEFAULT_MIN_XD_LEN,
) -> list[Segment]:
    """划分线段（状态机算法，移植自 yifangmoyan/Rust）。

    核心流程（对齐 Rust build_xd_impl）：
    1. 初始化 cur_seg（当前活跃线段）和 wait_confirm_seg（待确认线段）
    2. 逐笔推进，收集反向笔到特征序列
    3. 出现分型时：检查缺口 → 一类破坏(无缺口)直接终结 / 二类破坏(有缺口)启动确认
    4. 二类破坏等待反向线段走出分型完成二次确认
    5. 尾部未确认线段（前三笔重叠）也收入结果

    Args:
        strokes: 笔序列（由 identify_strokes 产出）
        min_len: 线段最少笔数，默认 3

    Returns:
        线段序列。
    """
    if len(strokes) < min_len:
        return []

    seg_list: list[_InnerSeg] = []

    # 初始化当前线段
    cur_seg = _InnerSeg(0, strokes)

    # 等待二次确认的临时线段
    wait_confirm_seg: _InnerSeg | None = None

    ptr = 1
    while ptr < len(strokes):
        curr_bi = strokes[ptr]
        curr_bi_high = max(curr_bi.start_value, curr_bi.end_value)
        curr_bi_low = min(curr_bi.start_value, curr_bi.end_value)

        # ── 存在等待二次确认的二类破坏线段 ──
        if wait_confirm_seg is not None:
            wait_confirm_seg.add_bi(ptr, strokes)
            proc_feat = _merge_feature_include(wait_confirm_seg.feature, wait_confirm_seg.is_up)

            # 观察反向新线段是否走出分型完成二次确认
            confirm_ok = (
                _is_top_fx(proc_feat) if wait_confirm_seg.is_up
                else _is_bottom_fx(proc_feat)
            )

            if confirm_ok:
                # 二次确认成功：原线段正式终结
                cur_seg.confirmed_end = True
                seg_list.append(cur_seg)
                # 切换为新线段
                cur_seg = wait_confirm_seg
                wait_confirm_seg = None
            else:
                # 二次确认失败检查：是否再创极值，原线段延续
                last_bi = strokes[cur_seg.bi_indices[-1]]
                last_high = max(last_bi.start_value, last_bi.end_value)
                last_low = min(last_bi.start_value, last_bi.end_value)

                if cur_seg.is_up and curr_bi_high > last_high:
                    # 创新高，原线段继续延伸，作废本次破坏信号
                    wait_confirm_seg = None
                elif not cur_seg.is_up and curr_bi_low < last_low:
                    # 创新低，原线段继续延伸，作废本次破坏信号
                    wait_confirm_seg = None

            ptr += 1
            continue

        # ── 正常模式：加入当前线段 ──
        cur_seg.add_bi(ptr, strokes)
        proc_feature = _merge_feature_include(cur_seg.feature, cur_seg.is_up)

        # 检测是否出现破坏分型
        hit_fx = (
            _is_top_fx(proc_feature) if cur_seg.is_up
            else _is_bottom_fx(proc_feature)
        )

        if not hit_fx:
            ptr += 1
            continue

        # 出现分型，区分一类/二类破坏
        gap = _feature_has_gap(proc_feature, cur_seg.is_up)

        if not gap:
            # ── 一类破坏：无缺口 → 直接终结 ──
            if cur_seg.bi_count() >= min_len:
                cur_seg.confirmed_end = True
                seg_list.append(cur_seg)
            # 新建反向线段（从当前笔开始，共享端点）
            cur_seg = _InnerSeg(ptr, strokes)
        else:
            # ── 二类破坏：有缺口 → 不直接终结，启动二次确认流程 ──
            wait_confirm_seg = _InnerSeg(ptr, strokes)

        ptr += 1

    # ── 尾部未确认完成的线段存入结果 ──
    if not cur_seg.confirmed_end and cur_seg.bi_count() >= min_len:
        if _check_overlap_of_first_3(strokes, cur_seg.bi_indices):
            seg_list.append(cur_seg)

    # 如果没划分出任何线段，但笔数>=min_len且前三笔重叠，整体作为一段
    if not seg_list and len(strokes) >= min_len:
        first3_indices = list(range(min_len))
        if _check_overlap_of_first_3(strokes, first3_indices):
            seg_dir = strokes[0].direction
            return [Segment(
                direction=seg_dir,
                start_index=0,
                end_index=len(strokes) - 1,
                start_date=strokes[0].start_date,
                end_date=strokes[-1].end_date,
                start_value=strokes[0].start_value,
                end_value=strokes[-1].end_value,
                strokes=strokes[:],
            )]

    # ── 转换为输出格式 ──
    return [_segment_to_output(seg, strokes) for seg in seg_list]
