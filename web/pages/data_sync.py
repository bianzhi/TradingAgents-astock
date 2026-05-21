"""数据同步子页面 — 多源K线数据缓存管理。"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st
from tradingagents.dataflows.a_stock import (
    resolve_ticker,
    sync_stock_data,
    get_cached_stocks,
    delete_cache,
)

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="数据同步 — TradingAgents-Astock",
    page_icon="💾",
    layout="wide",
)

# ── Custom CSS (same theme as main app) ──────────────────────────────────────

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap');
    #MainMenu, header[data-testid="stHeader"],
    footer, div[data-testid="stDecoration"],
    div[data-testid="stToolbar"] { display: none !important; }
    button[data-testid="collapsedControl"] { display: flex !important; }
    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, sans-serif;
    }
    .stApp { background: #0a0a0a; }
    section[data-testid="stSidebar"] {
        background: #0f0f0f;
        border-right: 1px solid #1a1a1a;
    }
    button[kind="primary"] {
        background: linear-gradient(135deg, #ff5a1f, #ff8c42) !important;
        border: none !important;
        font-weight: 700 !important;
    }
    .stDataFrame { background: #161616; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Navigation back ──────────────────────────────────────────────────────────

col_nav1, col_nav2 = st.columns([6, 1])
with col_nav2:
    if st.button("← 返回分析", use_container_width=True):
        st.switch_page("app.py")


# ── Header ───────────────────────────────────────────────────────────────────

st.markdown(
    """
    <div style="margin-bottom:1.5rem;">
        <span style="font-size:2rem; font-weight:900; color:#ff5a1f;">💾</span>
        <span style="font-size:1.5rem; font-weight:800; color:#f5f1eb;">数据同步</span>
        <div style="font-size:0.85rem; color:#888; margin-top:0.3rem;">
            四大 A 股数据源自动协同，主源失败自动切换备源
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Data sources table
st.markdown(
    """
    | 数据源 | 级别支持 | 特点 | 最大条数 |
    |--------|----------|------|----------|
    | 东方财富 | 1分~日线 | 分钟级最优 | 10,000 |
    | 新浪财经 | 1分~日线 | 速度快 | 2,000 |
    | 腾讯财经 | 1分~周线 | 稳定 | 2,000 |
    | Tushare | 1分~月线 | 数据质量最高 | — |
    """,
    unsafe_allow_html=True,
)

st.caption(
    "同步策略：分钟级 东财→新浪→腾讯→Tushare ｜ "
    "日线/周线/月线 新浪→腾讯→Tushare→东财 ｜ "
    "周线/月线优先直接获取，失败则从日线重采样"
)


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_sync, tab_cache = st.tabs(["🔄 同步数据", "📂 缓存管理"])


# ── Tab 1: Sync data ────────────────────────────────────────────────────────

with tab_sync:
    st.markdown("#### 单股同步")
    st.caption("输入股票代码，选择K线级别和起始日期，自动从四大源获取数据")

    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        sync_input = st.text_input(
            "股票代码",
            placeholder="例: 600519 或 贵州茅台",
            key="sync_input",
        )
    with col2:
        level_options = {
            "日线": "daily", "周线": "weekly", "月线": "monthly",
            "1分钟": "1min", "5分钟": "5min", "15分钟": "15min",
            "30分钟": "30min", "60分钟": "60min",
        }
        level_label = st.selectbox(
            "K线级别",
            options=list(level_options.keys()),
            index=0,
            key="sync_level",
        )
        level = level_options[level_label]
    with col3:
        datalen = st.number_input(
            "K线数量",
            min_value=100,
            max_value=10000,
            value=10000,
            step=500,
            key="sync_datalen",
        )

    col_date1, col_date2 = st.columns(2)
    with col_date1:
        start_date = st.date_input(
            "起始日期",
            value=None,
            key="sync_start_date",
            help="留空则使用全局配置（默认20240101）",
        )
    with col_date2:
        st.markdown(
            '<div style="height:2.5rem"></div>',
            unsafe_allow_html=True,
        )
        st.caption(
            "💡 起始日期可通过环境变量 `TRADINGAGENTS_KLINE_START_DATE` 全局配置，\\n"
            "或在代码中 `set_config({\"kline_start_date\": \"2020-01-01\"})` 设置"
        )

    if st.button("开始同步", type="primary", use_container_width=True, disabled=not sync_input):
        # Resolve ticker
        resolved_code = None
        try:
            resolved_code = resolve_ticker(sync_input.strip())
            if resolved_code != sync_input.strip():
                st.info(f"✅ {sync_input.strip()} → {resolved_code}")
        except ValueError as e:
            st.error(f"❌ 无法解析股票代码：{e}")
            resolved_code = None

        if resolved_code:
            with st.spinner(f"正在同步 {resolved_code} {level_label}数据..."):
                result = sync_stock_data(
                    resolved_code,
                    level=level,
                    datalen=datalen,
                    start_date=str(start_date) if start_date else None,
                )

            if result["status"] == "ok":
                st.success(
                    f"✅ 同步成功！{result['code']} · {level_label} · "
                    f"{result['rows']} 根K线 · "
                    f"来源: {result['source']} · "
                    f"范围: {result['date_range'][0]} ~ {result['date_range'][1]}"
                )
            else:
                st.error(f"❌ 同步失败：{result.get('error', '未知错误')}")

    # ── Batch sync ───────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 批量同步")
    st.caption("多只股票用逗号分隔，批量同步日线数据")

    batch_input = st.text_area(
        "股票代码列表",
        placeholder="例: 600519,000858,300750",
        key="batch_input",
    )

    col_batch1, col_batch2, col_batch3 = st.columns([2, 1, 1])
    with col_batch1:
        batch_start = st.date_input(
            "批量起始日期",
            value=None,
            key="batch_start_date",
        )
    with col_batch2:
        batch_datalen = st.number_input(
            "K线数量",
            min_value=100,
            max_value=10000,
            value=10000,
            step=500,
            key="batch_datalen",
        )
    with col_batch3:
        batch_level_label = st.selectbox(
            "级别",
            options=list(level_options.keys()),
            index=0,
            key="batch_level",
        )

    if st.button("批量同步", type="primary", use_container_width=True, disabled=not batch_input):
        codes = [c.strip() for c in batch_input.split(",") if c.strip()]
        batch_level = level_options[batch_level_label]

        for raw_code in codes:
            try:
                resolved_code = resolve_ticker(raw_code)
                with st.spinner(f"同步 {resolved_code}..."):
                    result = sync_stock_data(
                        resolved_code,
                        level=batch_level,
                        datalen=batch_datalen,
                        start_date=str(batch_start) if batch_start else None,
                    )
                if result["status"] == "ok":
                    st.success(
                        f"✅ {resolved_code}: {result['rows']} 根 · "
                        f"来源:{result['source']} · "
                        f"{result['date_range'][0]}~{result['date_range'][1]}"
                    )
                else:
                    st.warning(f"⚠️ {resolved_code}: {result.get('error', '未知错误')}")
            except ValueError as e:
                st.error(f"❌ {raw_code}: {e}")


# ── Tab 2: Cache management ─────────────────────────────────────────────────

with tab_cache:
    cached = get_cached_stocks()

    if not cached:
        st.info("暂无缓存数据。请先在「同步数据」标签页同步股票数据。")
    else:
        st.markdown(f"共 **{len(cached)}** 条缓存记录")

        for item in cached:
            code = item.get("code", "?")
            level = item.get("level", "daily")
            rows = item.get("rows", 0)
            date_range = item.get("date_range", [])
            last_sync = item.get("last_sync", "未知")
            source = item.get("source", "未知")
            start = item.get("start_date", "")

            level_cn = {
                "daily": "日线", "weekly": "周线", "monthly": "月线",
                "1min": "1分钟", "5min": "5分钟", "15min": "15分钟",
                "30min": "30分钟", "60min": "60分钟",
            }.get(level, level)

            with st.container():
                col_info, col_action = st.columns([4, 1])

                with col_info:
                    parts = [f"**{code}** · {level_cn} · {rows} 根"]
                    if date_range and len(date_range) == 2:
                        year_span = calc_year_span(date_range[0], date_range[1])
                        parts.append(f"{date_range[0]} ~ {date_range[1]} ({year_span:.1f}年)")
                    if source:
                        parts.append(f"来源:{source}")
                    if start:
                        parts.append(f"起始:{start}")
                    parts.append(f"同步:{last_sync}")
                    st.markdown(" · ".join(parts))

                with col_action:
                    if st.button("🗑️", key=f"del_{code}_{level}",
                                 help=f"删除 {code} {level_cn} 缓存"):
                        if delete_cache(code, level=level):
                            st.success(f"已删除 {code} {level_cn}缓存")
                            st.rerun()
                        else:
                            st.warning(f"未找到 {code} {level} 的缓存文件")

                st.markdown("---")


def calc_year_span(start: str, end: str) -> float:
    """计算两个日期之间的年数跨度。"""
    try:
        from datetime import datetime
        s = datetime.strptime(str(start)[:10], "%Y-%m-%d")
        e = datetime.strptime(str(end)[:10], "%Y-%m-%d")
        return (e - s).days / 365.25
    except Exception:
        return 0.0
