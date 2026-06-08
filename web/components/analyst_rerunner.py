"""独立运行单个分析师并返回报告。

不依赖 LangGraph 图结构，直接手动执行 agent 工具调用循环：
analyst → tools → analyst → ... → 报告文本
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_fundamentals_analyst,
    create_hot_money_tracker,
    create_lockup_watcher,
    create_market_analyst,
    create_news_analyst,
    create_policy_analyst,
    create_social_media_analyst,
)
from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_global_news,
    get_insider_transactions,
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)
from tradingagents.llm_clients.factory import create_llm_client

logger = logging.getLogger(__name__)

# ── 分析师创建函数映射 ─────────────────────────────────────────────────────────

_ANALYST_CREATORS: Dict[str, Callable] = {
    "market":       create_market_analyst,
    "social":       create_social_media_analyst,
    "news":         create_news_analyst,
    "fundamentals": create_fundamentals_analyst,
    "policy":       create_policy_analyst,
    "hot_money":    create_hot_money_tracker,
    "lockup":       create_lockup_watcher,
}

# ── 每个分析师的工具集 ─────────────────────────────────────────────────────────

_ANALYST_TOOLS: Dict[str, List[Callable]] = {
    "market":       [get_stock_data, get_indicators],
    "social":       [get_news],
    "news":         [get_news, get_global_news, get_insider_transactions],
    "fundamentals": [
        get_fundamentals,
        get_balance_sheet,
        get_cashflow,
        get_income_statement,
        get_profit_forecast,
        get_industry_comparison,
    ],
    "policy":       [get_news, get_global_news],
    "hot_money":    [
        get_stock_data,
        get_news,
        get_insider_transactions,
        get_hot_stocks,
        get_northbound_flow,
        get_concept_blocks,
        get_fund_flow,
        get_dragon_tiger_board,
        get_industry_comparison,
    ],
    "lockup":       [get_insider_transactions, get_news, get_fundamentals, get_lockup_expiry],
}

# ── 分析师报告键映射 ───────────────────────────────────────────────────────────

_ANALYST_REPORT_KEYS: Dict[str, str] = {
    "market":       "market_report",
    "social":       "sentiment_report",
    "news":         "news_report",
    "fundamentals": "fundamentals_report",
    "policy":       "policy_report",
    "hot_money":    "hot_money_report",
    "lockup":       "lockup_report",
}

# ── 中文名称 ───────────────────────────────────────────────────────────────────

_ANALYST_CN_NAMES: Dict[str, str] = {
    "market":       "技术分析",
    "social":       "市场情绪",
    "news":         "新闻舆情",
    "fundamentals": "基本面",
    "policy":       "政策分析",
    "hot_money":    "游资追踪",
    "lockup":       "解禁监控",
}

# ═══════════════════════════════════════════════════════════════════════════════
#  公开 API
# ═══════════════════════════════════════════════════════════════════════════════

def run_single_analyst(
    analyst_type: str,
    ticker: str,
    trade_date: str,
    config: Dict[str, Any],
    max_iterations: int = 20,
) -> str:
    """独立运行单个分析师，返回分析报告文本。

    Args:
        analyst_type: 分析师类型（"market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"）
        ticker: 股票代码
        trade_date: 分析日期
        config: 配置字典（含 llm_provider, quick_think_llm, deep_think_llm 等）
        max_iterations: 最大工具调用轮数

    Returns:
        分析报告文本，失败时返回空字符串
    """
    if analyst_type not in _ANALYST_CREATORS:
        raise ValueError(f"Unknown analyst type: {analyst_type}")

    # ── 创建 LLM ──
    llm_kwargs: Dict[str, Any] = {}
    provider = config.get("llm_provider", "").lower()
    if provider == "google":
        level = config.get("google_thinking_level")
        if level:
            llm_kwargs["thinking_level"] = level
    elif provider == "openai":
        effort = config.get("openai_reasoning_effort")
        if effort:
            llm_kwargs["reasoning_effort"] = effort
    elif provider == "anthropic":
        effort = config.get("anthropic_effort")
        if effort:
            llm_kwargs["effort"] = effort

    client = create_llm_client(
        provider=config["llm_provider"],
        model=config["quick_think_llm"],
        base_url=config.get("backend_url"),
        **llm_kwargs,
    )
    llm = client.get_llm()

    # ── 创建分析师节点 ──
    create_fn = _ANALYST_CREATORS[analyst_type]
    analyst_node = create_fn(llm)

    # ── 创建工具节点 ──
    tools = list(_ANALYST_TOOLS[analyst_type])
    # market 分析师额外加缠论工具
    if analyst_type == "market":
        try:
            from tradingagents.agents.utils.chanlun_tools import get_chanlun_analysis
            tools.append(get_chanlun_analysis)
        except Exception:
            pass
    tool_node = ToolNode(tools)

    # ── 构建初始状态 ──
    state: Dict[str, Any] = {
        "messages": [HumanMessage(content=ticker)],
        "company_of_interest": ticker,
        "trade_date": str(trade_date),
    }

    report_key = _ANALYST_REPORT_KEYS[analyst_type]

    # ── Agent 工具调用循环 ──
    for _ in range(max_iterations):
        try:
            result = analyst_node(state)
        except Exception as exc:
            logger.error("Analyst %s invocation failed: %s", analyst_type, exc)
            break

        # 合并结果到状态
        for k, v in result.items():
            if k == "messages":
                state["messages"].extend(v)
            else:
                state[k] = v

        # 检查是否有报告产出
        report = result.get(report_key, "")
        if report:
            return str(report)

        # 检查是否需要调用工具
        last_msg = state["messages"][-1]
        if not getattr(last_msg, "tool_calls", None):
            # 没有工具调用也没有报告 — 可能是 LLM 输出了最终回复但 key 名不对
            # 尝试从消息内容获取
            content = getattr(last_msg, "content", "")
            if content:
                return str(content)
            break

        # 执行工具
        try:
            tool_result = tool_node.invoke(state)
        except Exception as exc:
            logger.error("Tool execution failed for %s: %s", analyst_type, exc)
            break

        if "messages" in tool_result:
            state["messages"].extend(tool_result["messages"])

    # 兜底：从最后的 AI 消息提取内容
    for msg in reversed(state.get("messages", [])):
        if hasattr(msg, "content") and msg.content and not isinstance(msg, HumanMessage) and not isinstance(msg, ToolMessage):
            return str(msg.content)

    return ""


def get_analyst_cn_name(analyst_type: str) -> str:
    """返回分析师的中文名称。"""
    return _ANALYST_CN_NAMES.get(analyst_type, analyst_type)


def get_report_key(analyst_type: str) -> str:
    """返回分析师对应的 state 报告键。"""
    return _ANALYST_REPORT_KEYS.get(analyst_type, "")
