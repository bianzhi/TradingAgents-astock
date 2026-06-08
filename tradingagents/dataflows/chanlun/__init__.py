"""缠论综合分析 — 一键入口。"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from .types import (
    ChanlunResult,
    ProcessedKLine,
)
from .core import process_inclusion, find_fractals, identify_strokes
from .segment import identify_segments
from .pivot import find_stroke_pivots, find_pivots, classify_trends
from .divergence import detect_divergence
from .buy_sell_points import find_buy_sell_points

logger = logging.getLogger(__name__)


def analyze(
    df: pd.DataFrame,
    symbol: str = "",
    curr_date: str = "",
    level: str = "daily",
) -> ChanlunResult:
    """对K线数据进行缠论综合分析。

    Args:
        df: K线数据，需包含 Date/Open/High/Low/Close/Volume 列
        symbol: 股票代码
        curr_date: 当前交易日
        level: 分析级别

    Returns:
        ChanlunResult 综合分析结果。
    """
    if df.empty:
        return ChanlunResult(symbol=symbol, curr_date=curr_date, level=level)

    # 1. K线包含处理
    processed = process_inclusion(df.to_dict("records"))

    # 2. 分型识别
    fractals = find_fractals(processed)

    # 3. 笔划分
    strokes = identify_strokes(processed, fractals)

    # 4. 线段划分
    segments = identify_segments(strokes)

    # 5. 笔中枢识别（直接基于笔，而非线段）
    pivots = find_stroke_pivots(strokes)

    # 6. 走势类型
    trends = classify_trends(segments, pivots)

    # 7. 背驰判断
    closes = df["Close"].tolist()
    close_dates = df["Date"].astype(str).tolist()
    divergences = detect_divergence(trends, segments, closes, close_dates, level)

    # 8. 买卖点
    buy_sell_points = find_buy_sell_points(
        divergences, trends, pivots, segments, closes, close_dates, level
    )

    return ChanlunResult(
        symbol=symbol,
        curr_date=curr_date,
        level=level,
        strokes=strokes,
        segments=segments,
        pivots=pivots,
        trends=trends,
        divergences=divergences,
        buy_sell_points=buy_sell_points,
    )


def _fmt_date(d) -> str:
    """格式化日期，去掉时间部分。"""
    s = str(d)
    return s.split(" ")[0] if " " in s else s


def format_result(result: ChanlunResult) -> str:
    """将缠论分析结果格式化为可读文本。"""
    lines: list[str] = []

    lines.append(f"# 缠论分析报告 — {result.symbol} — {_fmt_date(result.curr_date)}")
    lines.append("")

    # 走势概览
    lines.append("## 走势结构概览")
    lines.append(f"- 分析级别: {result.level}")
    lines.append(f"- 笔数量: {len(result.strokes)}")
    lines.append(f"- 线段数量: {len(result.segments)}")
    lines.append(f"- 中枢数量: {len(result.pivots)}")
    lines.append("")

    # 当前走势
    if result.trends:
        lines.append("## 走势类型")
        for t in result.trends[-3:]:  # 只显示最近3个走势
            type_cn = {
                "uptrend": "上涨趋势",
                "downtrend": "下跌趋势",
                "consolidation": "盘整",
            }.get(t.type.value, t.type.value)
            dir_cn = "↑" if t.direction.value == "up" else "↓"
            ext = "（延伸中）" if t.pivots and t.pivots[-1].is_extending else ""
            lines.append(
                f"- {_fmt_date(t.start_date)} ~ {_fmt_date(t.end_date)}: "
                f"{type_cn} {dir_cn}，含{len(t.pivots)}个中枢{ext}"
            )
        lines.append("")

    # 中枢详情
    if result.pivots:
        lines.append("## 中枢详情")
        lines.append("| # | ZG | ZD | GG | DD | 起始日 | 终止日 | 状态 |")
        lines.append("|---|-----|-----|-----|-----|--------|--------|------|")
        for i, p in enumerate(result.pivots[-5:], 1):  # 只显示最近5个
            status = "延伸中" if p.is_extending else "完成"
            lines.append(
                f"| {i} | {p.zg:.2f} | {p.zd:.2f} | {p.gg:.2f} | {p.dd:.2f} "
                f"| {_fmt_date(p.start_date)} | {_fmt_date(p.end_date)} | {status} |"
            )
        lines.append("")

    # 买卖点信号
    if result.buy_sell_points:
        lines.append("## 买卖点信号")
        lines.append("| 时间 | 类型 | 方向 | 价格 | 置信度 |")
        lines.append("|------|------|------|------|--------|")
        for bp in result.buy_sell_points:
            type_cn = {
                "1st_buy": "一买", "2nd_buy": "二买", "3rd_buy": "三买",
                "1st_sell": "一卖", "2nd_sell": "二卖", "3rd_sell": "三卖",
            }.get(bp.type.value, bp.type.value)
            direction = "买入" if "buy" in bp.type.value else "卖出"
            lines.append(
                f"| {_fmt_date(bp.date)} | {type_cn} | {direction} | {bp.value:.2f} | "
                f"{bp.confidence:.0%} |"
            )
        lines.append("")

    # 背驰判断
    if result.divergences:
        lines.append("## 背驰判断")
        for div in result.divergences:
            type_cn = "趋势背驰" if div.type.value == "trend" else "盘整背驰"
            dir_cn = "顶背驰" if div.direction.value == "up" else "底背驰"
            lines.append(
                f"- {_fmt_date(div.date)}: {type_cn} · {dir_cn}（力度: {div.strength:.0%}）"
            )
        lines.append("")

    # 综合判断
    lines.append("## 综合判断")
    summary = _generate_summary(result)
    lines.append(summary)

    return "\n".join(lines)


def _generate_summary(result: ChanlunResult) -> str:
    """生成综合判断文本。"""
    parts: list[str] = []

    # 当前走势状态
    if result.trends:
        current = result.trends[-1]
        type_cn = {
            "uptrend": "上涨趋势",
            "downtrend": "下跌趋势",
            "consolidation": "盘整",
        }.get(current.type.value, "未知")

        parts.append(f"当前日线处于{type_cn}中")

        if current.type.value == "uptrend":
            if current.pivots and current.pivots[-1].is_extending:
                parts.append("，最后一个中枢仍在延伸")
            else:
                parts.append("，c段延伸中")
        elif current.type.value == "downtrend":
            if current.pivots and current.pivots[-1].is_extending:
                parts.append("，最后一个中枢仍在延伸")
            else:
                parts.append("，c段延伸中")
        elif current.type.value == "consolidation":
            parts.append("，中枢震荡中")
    else:
        parts.append("数据不足，无法判断走势类型")

    # 买卖点提示
    if result.buy_sell_points:
        recent = result.buy_sell_points[-1]
        type_cn = {
            "1st_buy": "一买", "2nd_buy": "二买", "3rd_buy": "三买",
            "1st_sell": "一卖", "2nd_sell": "二卖", "3rd_sell": "三卖",
        }.get(recent.type.value, recent.type.value)
        direction = "买入" if "buy" in recent.type.value else "卖出"
        parts.append(f"。最近的信号是{_fmt_date(recent.date)}的{type_cn}{direction}信号")

    # 背驰提示
    if result.divergences:
        recent_div = result.divergences[-1]
        dir_cn = "顶背驰" if recent_div.direction.value == "up" else "底背驰"
        parts.append(f"。{_fmt_date(recent_div.date)}检测到{dir_cn}，力度{recent_div.strength:.0%}")

    return "".join(parts) + "。"
