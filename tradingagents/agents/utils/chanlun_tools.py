"""缠论分析 LangChain Tool — 封装供 Agent 调用。"""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import tool


@tool
def get_chanlun_analysis(
    symbol: Annotated[str, "6-digit A-stock code (e.g. 600519, 300750)"],
    curr_date: Annotated[str, "Current trading date, YYYY-mm-dd format"],
    look_back_days: Annotated[int, "Days of daily data to look back for analysis (default 2000)"] = 2000,
) -> str:
    """缠论综合分析 — 返回中枢、走势类型、背驰、买卖点的完整文本报告。

    基于「缠中说禅」理论，对A股标的进行日线级别缠论分析。
    分析内容：K线包含处理→分型识别→笔划分→线段→中枢→走势类型→背驰→买卖点。

    使用场景：当传统技术指标信号模糊或需额外验证时，缠论提供基于几何递归的独立分析视角。
    注意：缠论分析需要本地缓存中有充足的日线数据。如返回"数据不足"提示，请先在数据同步页面同步数据。
    """
    from tradingagents.dataflows.interface import route_to_vendor

    result = route_to_vendor(
        "get_chanlun_analysis",
        symbol=symbol,
        curr_date=curr_date,
        look_back_days=look_back_days,
    )
    return result
