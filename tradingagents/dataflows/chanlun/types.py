"""缠论（缠中说禅）技术分析引擎 — 类型定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Direction(Enum):
    UP = "up"
    DOWN = "down"


class FractalType(Enum):
    TOP = "top"
    BOTTOM = "bottom"


class TrendType(Enum):
    UPTREND = "uptrend"          # 上涨趋势：≥2个同向(向上)中枢，不重叠
    DOWNTREND = "downtrend"      # 下跌趋势：≥2个同向(向下)中枢，不重叠
    CONSOLIDATION = "consolidation"  # 盘整：1个中枢


class BuySellType(Enum):
    # 买点
    FIRST_BUY = "1st_buy"      # 一买：趋势底背驰
    SECOND_BUY = "2nd_buy"     # 二买：一买后次级别回抽
    THIRD_BUY = "3rd_buy"      # 三买：离开中枢后回试不破ZG
    # 卖点
    FIRST_SELL = "1st_sell"    # 一卖：趋势顶背驰
    SECOND_SELL = "2nd_sell"   # 二卖：一卖后次级别回抽
    THIRD_SELL = "3rd_sell"    # 三卖：离开中枢后回试不破ZD


class DivergenceType(Enum):
    TREND = "trend"            # 趋势背驰
    PATTERN = "pattern"        # 盘整背驰


@dataclass
class KLine:
    """原始K线。"""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass
class ProcessedKLine:
    """包含处理后的K线。merged_from 记录被合并的原始K线索引。"""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    merged_from: list[int] = field(default_factory=list)
    original_indices: list[int] = field(default_factory=list)
    """original_indices[0] 是该处理后K线对应的原始K线位置（用于回溯）。"""


@dataclass
class Fractal:
    """分型（顶/底）。"""
    type: FractalType
    index: int          # 处理后K线序列中的索引
    date: str
    value: float        # 顶分型=high, 底分型=low


@dataclass
class Stroke:
    """笔。"""
    direction: Direction
    start_index: int    # 处理后K线序列中的索引
    end_index: int
    start_date: str
    end_date: str
    start_value: float
    end_value: float


@dataclass
class Segment:
    """线段。"""
    direction: Direction
    start_index: int    # 笔序列中的索引
    end_index: int
    start_date: str
    end_date: str
    start_value: float
    end_value: float
    strokes: list[Stroke] = field(default_factory=list)


@dataclass
class Pivot:
    """中枢。"""
    zg: float           # 中枢上沿 = max(三段lows)
    zd: float           # 中枢下沿 = min(三段highs)
    gg: float           # 中枢最高点
    dd: float           # 中枢最低点
    start_date: str
    end_date: str
    start_index: int    # 线段序列中的索引
    end_index: int
    is_extending: bool = False  # 是否仍在延伸
    level: str = "daily"


@dataclass
class Trend:
    """走势类型。"""
    type: TrendType
    direction: Direction
    start_date: str
    end_date: str
    start_value: float
    end_value: float
    pivots: list[Pivot] = field(default_factory=list)


@dataclass
class Divergence:
    """背驰标记。"""
    type: DivergenceType
    direction: Direction     # TOP=顶背驰, DOWN=底背驰
    level: str
    date: str
    value: float
    strength: float = 0.0   # 背驰力度（0~1，越大越强）


@dataclass
class BuySellPoint:
    """买卖点。"""
    type: BuySellType
    date: str
    value: float
    level: str = "daily"
    confidence: float = 0.0  # 0~1
    related_pivot: Optional[Pivot] = None


@dataclass
class ChanlunResult:
    """缠论综合分析结果。"""
    symbol: str
    curr_date: str
    level: str = "daily"
    # 基础结构
    strokes: list[Stroke] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    pivots: list[Pivot] = field(default_factory=list)
    trends: list[Trend] = field(default_factory=list)
    # 交易信号
    divergences: list[Divergence] = field(default_factory=list)
    buy_sell_points: list[BuySellPoint] = field(default_factory=list)
