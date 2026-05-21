"""缠论核心模块单元测试：K线包含处理、分型识别、笔划分。"""

from __future__ import annotations

import pytest

from tradingagents.dataflows.chanlun.types import (
    Direction,
    FractalType,
    ProcessedKLine,
)
from tradingagents.dataflows.chanlun.core import (
    _has_inclusion,
    process_inclusion,
    find_fractals,
    identify_strokes,
)


def _make_kline(date, open_, high, low, close, volume=0):
    """快速构造字典K线。"""
    return {"Date": date, "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}


def _make_processed(date, high, low, open_=0, close=0, volume=0):
    """快速构造 ProcessedKLine。"""
    return ProcessedKLine(date=date, open=open_, high=high, low=low, close=close, volume=volume)


# ---------------------------------------------------------------------------
# K线包含处理
# ---------------------------------------------------------------------------

class TestInclusionProcessing:
    """测试K线包含处理。"""

    def test_no_inclusion(self):
        """不含包含关系的K线序列，处理后不变。"""
        klines = [
            _make_kline("2025-01-01", 10, 12, 8, 11),
            _make_kline("2025-01-02", 11, 14, 9, 13),
            _make_kline("2025-01-03", 13, 15, 10, 14),
        ]
        result = process_inclusion(klines)
        assert len(result) == 3

    def test_simple_upward_inclusion(self):
        """向上包含：取 max(high), max(low)。"""
        klines = [
            _make_kline("2025-01-01", 10, 12, 8, 11),   # 基准
            _make_kline("2025-01-02", 11, 11, 9, 10),     # 被1包含
            _make_kline("2025-01-03", 12, 16, 10, 15),    # 继续向上
        ]
        result = process_inclusion(klines)
        # K2被包含在K1中，向上合并取 max(high)=12, max(low)=9
        # 合并后K1': high=12, low=9
        # K3: high=16, low=10 与K1'无包含
        assert len(result) == 2
        # 第一根是合并后的
        assert result[0].high == 12
        assert result[0].low == 9

    def test_simple_downward_inclusion(self):
        """向下包含：取 min(high), min(low)。"""
        klines = [
            _make_kline("2025-01-01", 15, 16, 10, 11),   # 高点
            _make_kline("2025-01-02", 12, 14, 9, 10),     # 下跌且包含
            _make_kline("2025-01-03", 9, 10, 7, 8),       # 继续向下
        ]
        result = process_inclusion(klines)
        assert len(result) >= 2  # 至少2根处理后K线

    def test_consecutive_inclusion(self):
        """5根K线连续包含，逐步合并。"""
        klines = [
            _make_kline("2025-01-01", 10, 15, 5, 14),   # 宽幅
            _make_kline("2025-01-02", 11, 13, 7, 12),   # 被1包含
            _make_kline("2025-01-03", 10, 11, 8, 9),    # 被1包含
            _make_kline("2025-01-04", 12, 14, 6, 13),   # 被1包含
            _make_kline("2025-01-05", 16, 18, 4, 17),   # 不被包含
        ]
        result = process_inclusion(klines)
        # K1-K4都有包含关系，方向逐步确认
        assert len(result) < 5  # 至少有合并

    def test_empty_input(self):
        """空输入。"""
        assert process_inclusion([]) == []

    def test_single_kline(self):
        """单根K线。"""
        klines = [_make_kline("2025-01-01", 10, 12, 8, 11)]
        result = process_inclusion(klines)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 分型识别
# ---------------------------------------------------------------------------

class TestFractalFinding:
    """测试顶底分型识别。"""

    def test_top_fractal(self):
        """标准顶分型。"""
        klines = [
            _make_processed("2025-01-01", 10, low=10),   # 左
            _make_processed("2025-01-02", 15, low=12),   # 中（高点最高）
            _make_processed("2025-01-03", 11, low=9),    # 右
        ]
        fractals = find_fractals(klines)
        assert len(fractals) == 1
        assert fractals[0].type == FractalType.TOP
        assert fractals[0].value == 15

    def test_bottom_fractal(self):
        """标准底分型。"""
        klines = [
            _make_processed("2025-01-01", 11, low=9),    # 左
            _make_processed("2025-01-02", 8, low=6),     # 中（低点最低）
            _make_processed("2025-01-03", 12, low=10),   # 右
        ]
        fractals = find_fractals(klines)
        assert len(fractals) == 1
        assert fractals[0].type == FractalType.BOTTOM
        assert fractals[0].value == 6

    def test_no_fractal_monotonic(self):
        """单调上升序列无分型。"""
        klines = [
            _make_processed("2025-01-01", 10, low=8),
            _make_processed("2025-01-02", 12, low=10),
            _make_processed("2025-01-03", 14, low=12),
        ]
        fractals = find_fractals(klines)
        assert len(fractals) == 0

    def test_alternating_fractals(self):
        """交替顶底分型。"""
        klines = [
            _make_processed("D1", 10, low=8),     # 底
            _make_processed("D2", 15, low=13),    # 顶
            _make_processed("D3", 9, low=7),      # 底
            _make_processed("D4", 16, low=14),    # 顶
            _make_processed("D5", 8, low=6),      # 底
        ]
        fractals = find_fractals(klines)
        types = [f.type for f in fractals]
        # D2是顶 (15>10且15>9), D3是底 (9<15且9<16), D4是顶 (16>9且16>8)
        assert FractalType.TOP in types
        assert FractalType.BOTTOM in types

    def test_too_few_klines(self):
        """不足3根K线。"""
        assert find_fractals([_make_processed("D1", 10, low=8)]) == []
        assert find_fractals([
            _make_processed("D1", 10, low=8),
            _make_processed("D2", 12, low=10),
        ]) == []


# ---------------------------------------------------------------------------
# 笔划分
# ---------------------------------------------------------------------------

class TestStrokeIdentification:
    """测试笔划分。"""

    def test_basic_strokes(self):
        """基本交替顶底 → 正确连笔（分型间至少间隔3根K线）。"""
        # 构造：底→(3根K线)→顶→(3根K线)→底→(3根K线)→顶
        klines = [
            _make_processed("D1",  10, low=8),    # 0
            _make_processed("D2",  12, low=10),   # 1
            _make_processed("D3",  14, low=12),   # 2
            _make_processed("D4",  20, low=18),   # 3  顶分型中间
            _make_processed("D5",  17, low=15),   # 4
            _make_processed("D6",  15, low=13),   # 5
            _make_processed("D7",  13, low=11),   # 6
            _make_processed("D8",  10, low=8),    # 7  底分型中间
            _make_processed("D9",  12, low=10),   # 8
            _make_processed("D10", 14, low=12),   # 9
            _make_processed("D11", 16, low=14),   # 10
            _make_processed("D12", 22, low=20),   # 11 顶分型中间
            _make_processed("D13", 19, low=17),   # 12
        ]
        fractals = find_fractals(klines)
        strokes = identify_strokes(klines, fractals)

        # 应该有笔
        assert len(strokes) >= 1
        # 方向交替
        for i in range(1, len(strokes)):
            assert strokes[i].direction != strokes[i-1].direction

    def test_no_strokes_insufficient_klines(self):
        """K线不足无笔。"""
        klines = [_make_processed("D1", 10, low=8)]
        strokes = identify_strokes(klines)
        assert len(strokes) == 0

    def test_stroke_direction(self):
        """向下一笔：顶→底；向上一笔：底→顶（分型间至少间隔3根K线）。"""
        klines = [
            _make_processed("D1",  10, low=8),    # 0
            _make_processed("D2",  14, low=12),   # 1
            _make_processed("D3",  18, low=16),   # 2
            _make_processed("D4",  22, low=20),   # 3  顶
            _make_processed("D5",  18, low=16),   # 4
            _make_processed("D6",  14, low=12),   # 5
            _make_processed("D7",  10, low=8),    # 6
            _make_processed("D8",  8,  low=6),    # 7  底
            _make_processed("D9",  12, low=10),   # 8
            _make_processed("D10", 16, low=14),   # 9
            _make_processed("D11", 20, low=18),   # 10
            _make_processed("D12", 24, low=22),   # 11 顶
            _make_processed("D13", 20, low=18),   # 12
        ]
        fractals = find_fractals(klines)
        strokes = identify_strokes(klines, fractals)

        # 顶→底 = 向下，底→顶 = 向上
        for s in strokes:
            if s.start_value > s.end_value:
                assert s.direction == Direction.DOWN
            else:
                assert s.direction == Direction.UP
