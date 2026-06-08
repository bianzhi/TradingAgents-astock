"""缠论K线图：Plotly 交互式图表组件。

绘制原始K线 + 笔 + 笔中枢 + 背驰标记 + 三类买卖点标记。
不绘制线段。
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tradingagents.dataflows.chanlun import analyze as chanlun_analyze
from tradingagents.dataflows.chanlun.types import (
    BuySellType,
    Direction,
    Divergence,
    Pivot,
    Stroke,
)
from tradingagents.dataflows.kline_cache import load_kline

logger = logging.getLogger(__name__)

# ── 颜色主题 ──────────────────────────────────────────────────────────────────

_COLORS = {
    "bg": "#0a0a0a",
    "plot_bg": "#0f0f0f",
    "grid": "#1a1a1a",
    "text": "#aaaaaa",
    "up": "#26a69a",           # 阳线（青绿）
    "down": "#ef5350",         # 阴线（暖红）
    "stroke_line": "#fdd835",  # 笔连线（亮黄色，单色统一）
    "up_dot": "#00e676",       # 上升笔端点（亮绿）
    "down_dot": "#ff5252",     # 下降笔端点（亮红）
    "pivot_fill": "rgba(255, 152, 0, 0.20)",
    "pivot_border": "rgba(255, 152, 0, 0.72)",
    "div_top": "#ff1744",      # 顶背驰（深红）
    "div_bot": "#00e676",      # 底背驰（亮绿）
    "buy_1st": "#00e676",      # 一买（深绿）
    "buy_2nd": "#69f0ae",      # 二买（浅绿）
    "buy_3rd": "#b9f6ca",      # 三买（更浅绿）
    "sell_1st": "#ff1744",     # 一卖（深红）
    "sell_2nd": "#ff5252",     # 二卖（中红）
    "sell_3rd": "#ff8a80",     # 三卖（浅红）
    "volume_up": "rgba(38,166,154,0.40)",
    "volume_down": "rgba(239,83,80,0.40)",
}

# ── 买卖点类型映射 ────────────────────────────────────────────────────────────

_BUY_SELL_CONFIG = {
    BuySellType.FIRST_BUY:  ("一买", _COLORS["buy_1st"], "triangle-up", 10),
    BuySellType.SECOND_BUY: ("二买", _COLORS["buy_2nd"], "triangle-up", 8),
    BuySellType.THIRD_BUY:  ("三买", _COLORS["buy_3rd"], "triangle-up", 7),
    BuySellType.FIRST_SELL:  ("一卖", _COLORS["sell_1st"], "triangle-down", 10),
    BuySellType.SECOND_SELL: ("二卖", _COLORS["sell_2nd"], "triangle-down", 8),
    BuySellType.THIRD_SELL:  ("三卖", _COLORS["sell_3rd"], "triangle-down", 7),
}


# ═══════════════════════════════════════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════════════════════════════════════

def build_chanlun_figure(
    code: str,
    curr_date: str,
    level: str = "daily",
    days_back: int = 280,
) -> go.Figure | None:
    """构建缠论K线图，返回 Plotly Figure 或 None（数据不足）。

    Args:
        code: 6位股票代码
        curr_date: 当前日期 YYYY-mm-dd
        level: 分析级别（daily/30min/...）
        days_back: 向前回溯天数
    """
    # ── 加载K线 ──
    # 取更长时间范围用于缠论分析（需要足够的历史数据）
    df_full = _load_kline_safe(code, level, end_date=curr_date)
    if df_full.empty:
        logger.warning("No K-line data for %s %s", code, level)
        return None

    if len(df_full) < 30:
        logger.warning("Insufficient K-line data for %s: %d bars", code, len(df_full))
        return None

    # ── 缠论分析（用全量数据） ──
    result = chanlun_analyze(df_full, symbol=code, curr_date=curr_date, level=level)

    # ── 截取最近 N 天用于展示 ──
    if len(df_full) > days_back:
        df = df_full.iloc[-days_back:].reset_index(drop=True)
        cutoff_date = df["Date"].iloc[0]
        # pandas Timestamp
        cutoff_ts = pd.Timestamp(cutoff_date)
        if hasattr(cutoff_ts, 'tz_localize'):
            cutoff_ts = cutoff_ts.tz_localize(None)
    else:
        df = df_full
        cutoff_ts = None

    closes = df["Close"].tolist()
    dates = df["Date"].tolist()

    # ── 创建图表 ──
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.72, 0.28],
        subplot_titles=(f"{code} · {_level_cn(level)}", ""),
    )

    # ── 1. K线（主图） ──
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=closes,
            name="K线",
            increasing=dict(line=dict(color=_COLORS["up"], width=1),
                            fillcolor=_COLORS["up"]),
            decreasing=dict(line=dict(color=_COLORS["down"], width=1),
                            fillcolor=_COLORS["down"]),
            showlegend=True,
        ),
        row=1, col=1,
    )

    # ── 2. 笔（主图叠加） ──
    _add_strokes(fig, result.strokes, cutoff_ts, row=1)

    # ── 3. 中枢（主图填充矩形） ──
    _add_pivots(fig, result.pivots, cutoff_ts, row=1)

    # ── 4. 背驰标记 ──
    _add_divergences(fig, result.divergences, result.strokes, row=1)

    # ── 5. 买卖点标记 ──
    _add_buy_sell_points(fig, result.buy_sell_points, row=1)

    # ── 6. 成交量（副图） ──
    vol_colors = [
        _COLORS["volume_up"] if closes[i] >= df["Open"].iloc[i]
        else _COLORS["volume_down"]
        for i in range(len(closes))
    ]
    fig.add_trace(
        go.Bar(
            x=dates,
            y=df["Volume"],
            name="成交量",
            marker=dict(color=vol_colors, line=dict(width=0)),
            showlegend=True,
        ),
        row=2, col=1,
    )

    # ── 布局 ──
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_COLORS["bg"],
        plot_bgcolor=_COLORS["plot_bg"],
        font=dict(color=_COLORS["text"], size=11),
        margin=dict(l=10, r=30, t=40, b=10),
        height=680,
        hovermode="x unified",
        dragmode="pan",
        legend=dict(
            orientation="h",
            yanchor="top",
            y=1.12,
            xanchor="left",
            x=0,
            bgcolor="rgba(0,0,0,0)",
            font=dict(size=10),
        ),
        xaxis=dict(
            gridcolor=_COLORS["grid"],
            zeroline=False,
            showspikes=True,
            spikethickness=1,
            spikecolor=_COLORS["grid"],
            rangeslider=dict(visible=True, thickness=0.04, bgcolor="#1a1a1a"),
        ),
        yaxis=dict(
            gridcolor=_COLORS["grid"],
            zeroline=False,
            side="right",
            showspikes=True,
            spikethickness=1,
            spikecolor=_COLORS["grid"],
        ),
        xaxis2=dict(
            gridcolor=_COLORS["grid"],
            zeroline=False,
            rangeslider=dict(visible=False),
        ),
        yaxis2=dict(
            gridcolor=_COLORS["grid"],
            zeroline=False,
            side="right",
        ),
    )

    # 更新副图标题
    fig.update_annotations(
        selector=dict(text=""),
        text="",
    )

    return fig


# ═══════════════════════════════════════════════════════════════════════════════
#  图表绘制子函数
# ═══════════════════════════════════════════════════════════════════════════════

def _strokes_in_window(strokes: list[Stroke], cutoff_ts) -> list[Stroke]:
    """筛选可视窗口内的笔。"""
    if cutoff_ts is None:
        return strokes
    result = []
    for s in strokes:
        try:
            if pd.Timestamp(s.end_date) >= cutoff_ts:
                result.append(s)
        except Exception:
            result.append(s)
    return result


def _add_strokes(fig: go.Figure, strokes: list[Stroke], cutoff_ts, row: int = 1) -> None:
    """在K线图上叠加笔的连线。单色黄线，清晰可见。"""
    visible = _strokes_in_window(strokes, cutoff_ts)
    if len(visible) < 2:
        return

    # 所有笔用相同颜色绘制
    all_dates: list = []
    all_vals: list = []
    all_texts: list = []

    for s in visible:
        dir_cn = "↑" if s.direction == Direction.UP else "↓"
        all_dates.extend([s.start_date, s.end_date, None])
        all_vals.extend([s.start_value, s.end_value, None])
        all_texts.extend([
            f"笔 {dir_cn}<br>{s.start_date} → {s.end_date}<br>{s.start_value:.2f} → {s.end_value:.2f}",
            f"笔 {dir_cn}<br>{s.start_date} → {s.end_date}<br>{s.start_value:.2f} → {s.end_value:.2f}",
            "",
        ])

    if all_dates:
        fig.add_trace(
            go.Scatter(
                x=all_dates, y=all_vals,
                mode="lines",
                name="笔",
                line=dict(color=_COLORS["stroke_line"], width=2.5),
                connectgaps=False,
                hovertext=all_texts,
                hoverinfo="text",
                showlegend=True,
            ),
            row=row, col=1,
        )

    # 笔端点标记 — 上升笔端绿色，下降笔端红色
    end_dates = [s.end_date for s in visible]
    end_vals = [s.end_value for s in visible]
    end_colors = [
        _COLORS["up_dot"] if s.direction == Direction.UP else _COLORS["down_dot"]
        for s in visible
    ]
    fig.add_trace(
        go.Scatter(
            x=end_dates, y=end_vals,
            mode="markers",
            name="笔端点",
            marker=dict(color=end_colors, size=8, symbol="circle", line=dict(width=1, color="white")),
            hovertext=[f"{s.end_date}<br>{s.end_value:.2f}" for s in visible],
            hoverinfo="text",
            showlegend=False,
        ),
        row=row, col=1,
    )


def _add_pivots(fig: go.Figure, pivots: list[Pivot], cutoff_ts, row: int = 1) -> None:
    """绘制中枢矩形（ZG/ZD 区间 + GG/DD 虚线）。"""
    legend_added = False
    for p in pivots:
        try:
            if cutoff_ts is not None and pd.Timestamp(p.end_date) < cutoff_ts:
                continue
        except Exception:
            pass

        # ── ZG/ZD 主矩形 ──
        fig.add_trace(
            go.Scatter(
                x=[p.start_date, p.end_date, p.end_date, p.start_date, p.start_date],
                y=[p.zg, p.zg, p.zd, p.zd, p.zg],
                mode="lines",
                fill="toself",
                name="中枢" if not legend_added else "",
                legendgroup="pivot",
                showlegend=not legend_added,
                line=dict(width=0),
                fillcolor=_COLORS["pivot_fill"],
                hoveron="fills",
                hovertext=(
                    f"中枢<br>ZG={p.zg:.2f}  ZD={p.zd:.2f}<br>"
                    f"GG={p.gg:.2f}  DD={p.dd:.2f}<br>"
                    f"{str(p.start_date)[:10]} → {str(p.end_date)[:10]}"
                ),
                hoverinfo="text",
            ),
            row=row, col=1,
        )

        # ── ZG 上沿实线 ──
        fig.add_trace(
            go.Scatter(
                x=[p.start_date, p.end_date],
                y=[p.zg, p.zg],
                mode="lines",
                name="",
                legendgroup="pivot",
                showlegend=False,
                line=dict(color=_COLORS["pivot_border"], width=1, dash="solid"),
                hoverinfo="skip",
            ),
            row=row, col=1,
        )

        # ── ZD 下沿实线 ──
        fig.add_trace(
            go.Scatter(
                x=[p.start_date, p.end_date],
                y=[p.zd, p.zd],
                mode="lines",
                name="",
                legendgroup="pivot",
                showlegend=False,
                line=dict(color=_COLORS["pivot_border"], width=1, dash="solid"),
                hoverinfo="skip",
            ),
            row=row, col=1,
        )

        # ── GG 虚线 ──
        fig.add_trace(
            go.Scatter(
                x=[p.start_date, p.end_date],
                y=[p.gg, p.gg],
                mode="lines",
                name="",
                legendgroup="pivot",
                showlegend=False,
                line=dict(color=_COLORS["pivot_border"], width=0.7, dash="dot"),
                hoverinfo="skip",
            ),
            row=row, col=1,
        )

        # ── DD 虚线 ──
        fig.add_trace(
            go.Scatter(
                x=[p.start_date, p.end_date],
                y=[p.dd, p.dd],
                mode="lines",
                name="",
                legendgroup="pivot",
                showlegend=False,
                line=dict(color=_COLORS["pivot_border"], width=0.7, dash="dot"),
                hoverinfo="skip",
            ),
            row=row, col=1,
        )

        if not legend_added:
            legend_added = True


def _add_divergences(
    fig: go.Figure,
    divergences: list[Divergence],
    strokes: list[Stroke],
    row: int = 1,
) -> None:
    """标记背驰点：顶背驰=红向下三角，底背驰=绿向上三角。"""
    if not divergences:
        return

    type_cn = {"trend": "趋势背驰", "pattern": "盘整背驰"}

    for div in divergences:
        is_top = div.direction == Direction.UP
        marker_color = _COLORS["div_top"] if is_top else _COLORS["div_bot"]
        symbol = "triangle-down" if is_top else "triangle-up"
        dir_cn = "顶背驰" if is_top else "底背驰"

        # 在笔的端点位置标注（找最近的笔端点）
        label_x, label_y = div.date, div.value

        fig.add_trace(
            go.Scatter(
                x=[label_x],
                y=[label_y],
                mode="markers+text",
                name="背驰",
                legendgroup="divergence",
                showlegend=(div == divergences[0]),
                marker=dict(
                    color=marker_color,
                    size=14,
                    symbol=symbol,
                    line=dict(width=1, color="rgba(255,255,255,0.53)"),
                ),
                text=[f"    {dir_cn}"],
                textposition="top center" if is_top else "bottom center",
                textfont=dict(color=marker_color, size=11, family="Arial"),
                hovertext=(
                    f"{dir_cn} · {type_cn.get(div.type.value, div.type.value)}<br>"
                    f"日期: {str(div.date)[:10]}<br>"
                    f"价格: {div.value:.2f}<br>"
                    f"力度: {div.strength:.0%}"
                ),
                hoverinfo="text",
            ),
            row=row, col=1,
        )


def _add_buy_sell_points(
    fig: go.Figure,
    points,
    row: int = 1,
) -> None:
    """标记三类买卖点。"""
    if not points:
        return

    legend_shown: set = set()
    for pt in points:
        cfg = _BUY_SELL_CONFIG.get(pt.type)
        if cfg is None:
            continue

        label, color, symbol, size = cfg
        is_buy = "buy" in pt.type.value

        # 标注在对应价格位置
        fig.add_trace(
            go.Scatter(
                x=[pt.date],
                y=[pt.value],
                mode="markers+text",
                name=label,
                legendgroup=label,
                showlegend=label not in legend_shown,
                marker=dict(
                    color=color,
                    size=size,
                    symbol=symbol,
                    line=dict(width=1.5, color="rgba(255,255,255,0.8)"),
                ),
                text=[f"  {label}"],
                textposition="top center" if is_buy else "bottom center",
                textfont=dict(color=color, size=10, family="Arial"),
                hovertext=(
                    f"{label}<br>"
                    f"日期: {str(pt.date)[:10]}<br>"
                    f"价格: {pt.value:.2f}<br>"
                    f"置信度: {pt.confidence:.0%}"
                ),
                hoverinfo="text",
            ),
            row=row, col=1,
        )
        legend_shown.add(label)


# ═══════════════════════════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════════════════════════

def _load_kline_safe(code: str, level: str, end_date: str | None = None) -> pd.DataFrame:
    """安全加载K线，处理各种异常。"""
    try:
        df = load_kline(code, level, "stock", end_date=end_date)
        if df.empty:
            return df

        # 规范化列名
        df = df.rename(columns={
            "date": "Date", "open": "Open", "high": "High",
            "low": "Low", "close": "Close", "volume": "Volume",
        })

        # 确保 Date 列是 datetime
        if not pd.api.types.is_datetime64_any_dtype(df["Date"]):
            df["Date"] = pd.to_datetime(df["Date"])

        return df.sort_values("Date").reset_index(drop=True)
    except Exception as e:
        logger.error("Failed to load K-line for %s: %s", code, e)
        return pd.DataFrame()


def _level_cn(level: str) -> str:
    """级别中文名。"""
    return {
        "daily": "日线",
        "weekly": "周线",
        "monthly": "月线",
        "30min": "30分钟",
        "60min": "60分钟",
        "15min": "15分钟",
        "5min": "5分钟",
        "1min": "1分钟",
    }.get(level, level)


# ═══════════════════════════════════════════════════════════════════════════════
#  Streamlit 渲染入口
# ═══════════════════════════════════════════════════════════════════════════════

def render_chanlun_tab() -> None:
    """在 Streamlit 中渲染缠论K线图标签页。"""
    import streamlit as st

    st.markdown("### 📊 缠论K线图")

    from datetime import date, datetime

    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    with col1:
        ticker = st.text_input(
            "股票代码",
            placeholder="输入6位代码（如 600519）",
            key="chanlun_ticker",
        )
    with col2:
        start_date = st.date_input(
            "开始日期",
            value=date(2024, 1, 1),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
            key="chanlun_start_date",
        )
    with col3:
        level = st.selectbox(
            "级别",
            options=["daily", "30min", "60min", "weekly", "monthly"],
            format_func=_level_cn,
            key="chanlun_level",
        )
    with col4:
        end_date = st.date_input(
            "结束日期",
            value=date.today(),
            min_value=date(2000, 1, 1),
            max_value=date.today(),
            key="chanlun_end_date",
        )

    if not ticker or not ticker.strip():
        st.info("👈 输入股票代码并回车，查看缠论K线图")
        return

    code = ticker.strip()
    if len(code) != 6 or not code.isdigit():
        st.warning("请输入6位数字股票代码")
        return

    # 计算 calendar days（比实际多没关系，K线数据不足时会自然截断）
    days_back = (end_date - start_date).days + 1
    if days_back < 30:
        st.warning("日期范围至少需要30天")
        return

    with st.spinner(f"正在加载 {code} 的K线数据并计算缠论..."):
        curr_date = end_date.strftime("%Y-%m-%d")

        fig = build_chanlun_figure(
            code=code,
            curr_date=curr_date,
            level=level,
            days_back=days_back,
        )

    if fig is None:
        st.error(f"无法加载 {code} 的K线数据，请先在「数据同步」中同步该股票。")
        return

    st.plotly_chart(fig, use_container_width=True, key="chanlun_main_chart", config={
        "displayModeBar": True,
        "scrollZoom": True,
        "displaylogo": False,
        "doubleClick": "reset+autosize",
    })

    # ── 图例说明 ──
    st.caption(
        "📐 **笔**：黄线=笔连线 | "
        "🟧 **中枢**：橙色半透明矩形（ZG~ZD），虚线为 GG/DD | "
        "▼▲ **背驰**：红色倒三角=顶背驰，绿色正三角=底背驰 | "
        "🟢🔴 **买卖点**：绿三角=买点（深=一买，中=二买，浅=三买），"
        "红三角=卖点 | "
        "⌨️ **键盘**：点击图表聚焦后 ←→ 平移 · ↑↓ 纵移 · +- 缩放 · H 复位"
    )
