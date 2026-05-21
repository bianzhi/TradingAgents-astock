"""数据同步子页面 — 全量日线数据缓存管理。"""

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
            全量日线数据缓存管理 — 为缠论分析提供充足的历史数据
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ── Tabs ─────────────────────────────────────────────────────────────────────

tab_sync, tab_cache = st.tabs(["🔄 同步数据", "📂 缓存管理"])


# ── Tab 1: Sync data ────────────────────────────────────────────────────────

with tab_sync:
    st.markdown("#### 单股同步")
    st.caption("输入股票代码，拉取全量日线数据（覆盖上市至今，最多5000根K线）")

    col1, col2 = st.columns([3, 1])
    with col1:
        sync_input = st.text_input(
            "股票代码",
            placeholder="例: 600519 或 贵州茅台",
            key="sync_input",
        )
    with col2:
        datalen = st.number_input(
            "K线数量",
            min_value=100,
            max_value=5000,
            value=5000,
            step=500,
            key="sync_datalen",
        )

    if st.button("开始同步", type="primary", use_container_width=True, disabled=not sync_input):
        # Resolve ticker
        resolved_code = None
        try:
            resolved_code = resolve_ticker(sync_input.strip())
            if resolved_code != sync_input.strip():
                st.info(f"✅ {sync_input.strip()} → {resolved_code}")
        except ValueError as e:
            st.error(f"❌ {e}")

        if resolved_code:
            with st.spinner(f"正在同步 {resolved_code} 的日线数据..."):
                result = sync_stock_data(resolved_code, datalen=datalen)

            if result["status"] == "ok":
                st.success(
                    f"✅ {result['code']} 同步成功！"
                    f"共 {result['rows']} 根K线，"
                    f"日期范围: {result['date_range'][0]} ~ {result['date_range'][1]}"
                )
            else:
                st.error(f"❌ 同步失败: {result['error']}")

    st.markdown("---")
    st.markdown("#### 批量同步")
    st.caption("输入多只股票代码（逗号分隔），批量拉取日线数据")

    batch_input = st.text_area(
        "股票代码列表",
        placeholder="例: 600519,300750,000858",
        key="batch_input",
        height=100,
    )

    if st.button("批量同步", type="primary", use_container_width=True, disabled=not batch_input):
        codes = [c.strip() for c in batch_input.split(",") if c.strip()]
        resolved_codes = []

        for raw in codes:
            try:
                code = resolve_ticker(raw)
                resolved_codes.append(code)
            except ValueError as e:
                st.warning(f"⚠️ {raw}: {e}")

        if resolved_codes:
            progress = st.progress(0)
            status_text = st.empty()

            success_count = 0
            fail_count = 0

            for idx, code in enumerate(resolved_codes):
                status_text.text(f"正在同步 {code} ({idx+1}/{len(resolved_codes)})...")
                result = sync_stock_data(code, datalen=5000)

                if result["status"] == "ok":
                    success_count += 1
                    st.info(
                        f"✅ {code}: {result['rows']}根K线，"
                        f"{result['date_range'][0]} ~ {result['date_range'][1]}"
                    )
                else:
                    fail_count += 1
                    st.warning(f"❌ {code}: {result['error']}")

                progress.progress((idx + 1) / len(resolved_codes))

            st.success(f"批量同步完成！成功 {success_count} 只，失败 {fail_count} 只")


# ── Tab 2: Cache management ─────────────────────────────────────────────────

with tab_cache:
    # Refresh button
    col_refresh, col_spacer = st.columns([1, 5])
    with col_refresh:
        refresh = st.button("🔄 刷新", key="refresh_cache")

    cached = get_cached_stocks()

    if not cached:
        st.info("暂无缓存数据。请先在「同步数据」页面同步股票数据。")
    else:
        st.caption(f"共 {len(cached)} 只股票已缓存")
        st.markdown("")

        for item in cached:
            code = item.get("code", "???")
            rows = item.get("rows", 0)
            date_range = item.get("date_range", [])
            last_sync = item.get("last_sync", "未知")
            source = item.get("source", "未知")

            with st.container():
                col_info, col_action = st.columns([4, 1])

                with col_info:
                    if date_range and len(date_range) == 2:
                        year_span = calc_year_span(date_range[0], date_range[1])
                        st.markdown(
                            f"**{code}** — {rows} 根K线 · "
                            f"{date_range[0]} ~ {date_range[1]} "
                            f"({year_span:.1f}年) · "
                            f"来源: {source} · "
                            f"最后同步: {last_sync}"
                        )
                    else:
                        st.markdown(
                            f"**{code}** — {rows} 根K线 · "
                            f"来源: {source} · "
                            f"最后同步: {last_sync}"
                        )

                with col_action:
                    if st.button("🗑️", key=f"del_{code}", help=f"删除 {code} 的缓存"):
                        if delete_cache(code):
                            st.success(f"已删除 {code} 的缓存")
                            st.rerun()
                        else:
                            st.warning(f"未找到 {code} 的缓存文件")

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
