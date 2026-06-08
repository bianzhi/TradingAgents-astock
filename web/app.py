"""TradingAgents A股分析 — Streamlit Web UI."""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env")

# ── Monkey-patch: Starlette 1.0 + Streamlit 1.57 compat ────────────────────
# Starlette 1.0 changed receive_bytes() to raise KeyError (not RuntimeError)
# when a text frame arrives. Streamlit's WS handler only caught RuntimeError,
# causing unhandled KeyError → WS crash → browser black screen.
from importlib.metadata import version as _pkg_ver
from packaging.version import Version as _V

_starlette_ver = _V(_pkg_ver("starlette"))
if _starlette_ver >= _V("1.0"):
    try:
        from starlette.websockets import WebSocket as _WS

        _orig_receive_bytes = _WS.receive_bytes

        async def _receive_bytes_compat(self) -> bytes:
            """Patched receive_bytes that raises RuntimeError (not KeyError) on text frames."""
            try:
                return await _orig_receive_bytes(self)
            except KeyError:
                # Starlette 1.0+ raises KeyError when a text frame arrives
                # because message lacks the "bytes" key. Raise RuntimeError
                # instead so Streamlit's existing except clause catches it.
                raise RuntimeError(
                    "Expected binary websocket frame, received text frame"
                )

        _WS.receive_bytes = _receive_bytes_compat
    except Exception as _e:
        import warnings
        warnings.warn(f"Starlette 1.0 compat patch failed: {_e}", stacklevel=2)
# ── End monkey-patch ────────────────────────────────────────────────────────

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402

from web.components.progress_panel import render_progress  # noqa: E402
from web.components.report_viewer import render_report  # noqa: E402
from web.components.sidebar import render_sidebar  # noqa: E402
from web.components.chanlun_chart import render_chanlun_tab  # noqa: E402
from web.history import extract_signal, load_analysis  # noqa: E402
from web.progress import ProgressTracker  # noqa: E402
from web.runner import run_analysis_in_thread  # noqa: E402

# Data sync imports
from tradingagents.dataflows.a_stock import (  # noqa: E402
    resolve_ticker,
    sync_stock_data,
    get_cached_stocks,
    delete_cache,
    sync_sector_data,
    get_sector_list,
    get_cached_sectors,
    delete_sector_cache,
    sync_index_data,
    get_index_list,
    get_cached_indices,
    delete_index_cache,
)

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="TradingAgents-Astock A股分析",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&display=swap');

    /* Hide Streamlit chrome for clean video recording */
    #MainMenu, header[data-testid="stHeader"],
    footer, div[data-testid="stDecoration"],
    div[data-testid="stToolbar"] { display: none !important; }
    /* Ensure sidebar collapse/expand control is always visible */
    button[data-testid="collapsedControl"] { display: flex !important; }

    html, body, [class*="css"] {
        font-family: 'Inter', -apple-system, sans-serif;
    }
    .stApp {
        background: #0a0a0a;
    }
    section[data-testid="stSidebar"] {
        background: #0f0f0f;
        border-right: 1px solid #1a1a1a;
    }
    .stMetric label { color: #888 !important; font-size: 0.8rem !important; }
    .stMetric [data-testid="stMetricValue"] {
        color: #ff5a1f !important;
        font-weight: 700 !important;
    }
    .stProgress > div > div > div {
        background: linear-gradient(90deg, #ff5a1f, #ff8c42) !important;
    }
    button[kind="primary"] {
        background: linear-gradient(135deg, #ff5a1f, #ff8c42) !important;
        border: none !important;
        font-weight: 700 !important;
        letter-spacing: 0.05em !important;
        box-shadow: 0 4px 15px rgba(255,90,31,0.3) !important;
        transition: all 0.2s ease !important;
    }
    button[kind="primary"]:hover {
        background: linear-gradient(135deg, #e04d15, #ff5a1f) !important;
        box-shadow: 0 6px 20px rgba(255,90,31,0.4) !important;
        transform: translateY(-1px) !important;
    }
    /* Secondary buttons (history items) */
    button[kind="secondary"] {
        background: #161616 !important;
        border: 1px solid #2a2a2a !important;
        color: #ccc !important;
        transition: all 0.2s ease !important;
    }
    button[kind="secondary"]:hover {
        background: #1e1e1e !important;
        border-color: #ff5a1f !important;
        color: #ff5a1f !important;
    }
    .stExpander {
        border: 1px solid #222 !important;
        border-radius: 8px !important;
    }
    .stTabs [data-baseweb="tab"] {
        color: #888 !important;
    }
    .stTabs [aria-selected="true"] {
        color: #ff5a1f !important;
        border-bottom-color: #ff5a1f !important;
    }
    div[data-testid="stDownloadButton"] button {
        background: #1a1a2e !important;
        border: 1px solid #ff5a1f !important;
        color: #ff5a1f !important;
    }
    /* Text input styling */
    input[data-testid="stTextInputRootElement"] input,
    .stTextInput input {
        background: #161616 !important;
        border-color: #2a2a2a !important;
        color: #f5f1eb !important;
    }
    .stTextInput input:focus {
        border-color: #ff5a1f !important;
        box-shadow: 0 0 0 1px #ff5a1f !important;
    }
    /* Date input styling */
    .stDateInput input {
        background: #161616 !important;
        border-color: #2a2a2a !important;
        color: #f5f1eb !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Build config ─────────────────────────────────────────────────────────────

def _build_config() -> dict:
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = st.session_state.get("llm_provider", "minimax")
    config["deep_think_llm"] = st.session_state.get("deep_think_llm", "MiniMax-M2.7")
    config["quick_think_llm"] = st.session_state.get("quick_think_llm", "MiniMax-M2.7-highspeed")
    config["data_vendors"] = {
        "core_stock_apis": "a_stock",
        "technical_indicators": "a_stock",
        "fundamental_data": "a_stock",
        "news_data": "a_stock",
        "signal_data": "a_stock",
    }
    config["max_debate_rounds"] = 1
    config["max_risk_discuss_rounds"] = 1
    config["output_language"] = "Chinese"
    return config


# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    render_sidebar()


# ── Main tabs ────────────────────────────────────────────────────────────────

tab_analysis, tab_chanlun, tab_datasync = st.tabs([
    "📈 投研分析", "📊 缠论K线", "💾 数据同步",
])


# ── Tab 1: 投研分析 (original state machine) ───────────────────────────────

with tab_analysis:
    # Handle "Start Analysis" trigger
    start_req = st.session_state.pop("start_analysis", None)
    if start_req:
        st.session_state.pop("report_overrides", None)  # clear stale overrides
        tracker = ProgressTracker(
            ticker=start_req["ticker"],
            trade_date=start_req["trade_date"],
        )
        st.session_state["tracker"] = tracker
        run_analysis_in_thread(
            ticker=start_req["ticker"],
            trade_date=start_req["trade_date"],
            config=_build_config(),
            tracker=tracker,
        )

    tracker: ProgressTracker | None = st.session_state.get("tracker")
    viewing_history: str | None = st.session_state.get("viewing_history")

    # ── Handle analyst rerun request ──────────────────────────────────────
    pending_rerun: str | None = st.session_state.pop("pending_rerun", None)
    if pending_rerun:
        from web.components.analyst_rerunner import run_single_analyst, get_report_key, get_analyst_cn_name

        # Determine ticker/trade_date from current context
        rerun_ticker = ""
        rerun_date = ""
        if tracker and tracker.ticker:
            rerun_ticker = tracker.ticker
            rerun_date = tracker.trade_date
        elif viewing_history:
            rerun_ticker = Path(viewing_history).parent.parent.name
            rerun_date = Path(viewing_history).stem.replace("full_states_log_", "")

        if rerun_ticker and rerun_date:
            cn_name = get_analyst_cn_name(pending_rerun)
            with st.spinner(f"🔄 正在重新运行「{cn_name}」分析..."):
                new_report = run_single_analyst(
                    pending_rerun, rerun_ticker, rerun_date, _build_config()
                )
            if new_report:
                report_key = get_report_key(pending_rerun)
                st.session_state.setdefault("report_overrides", {})[report_key] = new_report
                st.success(f"✅「{cn_name}」重新分析完成")
            else:
                st.warning(f"⚠️「{cn_name}」重新分析未产生有效报告")
        else:
            st.warning("无法确定分析上下文，请先完成一次完整分析")
        # Don't rerun yet — let state machine continue to render

    # Merge any overrides from re-runs
    overrides = st.session_state.get("report_overrides", {})

    # State 1: Viewing a historical analysis
    if viewing_history:
        try:
            state = load_analysis(viewing_history)
            signal = extract_signal(state)
            ticker = Path(viewing_history).parent.parent.name
            trade_date = Path(viewing_history).stem.replace("full_states_log_", "")
            render_report({**state, **overrides}, ticker, trade_date, signal)
        except Exception as exc:
            st.error(f"加载失败: {exc}")

    # State 2: Analysis running
    elif tracker and tracker.is_running:
        render_progress(tracker)
        time.sleep(2)
        st.rerun()

    # State 3: Analysis complete
    elif tracker and tracker.is_complete:
        render_report(
            {**tracker.final_state, **overrides},
            tracker.ticker,
            tracker.trade_date,
            tracker.signal,
            elapsed=tracker.elapsed,
        )

    # State 4: Analysis errored
    elif tracker and tracker.error:
        st.error(f"分析失败: {tracker.error}")
        if getattr(tracker, "partial_state", None):
            with st.expander("📡 已完成的部分分析", expanded=True):
                st.caption("_⚠️ 以下为出错前已完成的部分结果，可能不完整_")
                render_report(
                    {**tracker.partial_state, **overrides},
                    tracker.ticker,
                    tracker.trade_date,
                    tracker.signal or "N/A",
                )
        if st.button("重试"):
            st.session_state.pop("tracker", None)
            st.rerun()

    # State 0: Idle — welcome screen
    else:
        st.markdown(
            """
            <div style="
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                min-height: 60vh;
                text-align: center;
            ">
                <div style="font-size: 4rem; margin-bottom: 1rem;">📈</div>
                <div style="
                    font-size: 2.5rem;
                    font-weight: 900;
                    margin-bottom: 0.5rem;
                ">
                    <span style="color: #ff5a1f;">Trading</span><span style="color: #f5f1eb;">Agents</span><span style="color: #f5f1eb;">-</span><span style="color: #ff5a1f;">Astock</span>
                </div>
                <div style="color: #888; font-size: 1.1rem; max-width: 500px; line-height: 1.6;">
                    A股多Agent投研分析系统<br>
                    7位AI分析师 → 质量门控 → 多空辩论 → 风控评估 → 最终决策
                </div>
                <div style="
                    margin-top: 2rem;
                    padding: 1rem 2rem;
                    border: 1px solid #222;
                    border-radius: 12px;
                    color: #666;
                    font-size: 0.9rem;
                ">
                    ← 在左侧输入股票代码，开始分析
                </div>
                <div style="
                    margin-top: 2.5rem;
                    padding: 0.8rem 1.5rem;
                    color: #555;
                    font-size: 0.75rem;
                    max-width: 500px;
                    line-height: 1.6;
                    border-top: 1px solid #1a1a1a;
                ">
                    ⚠️ 本项目仅供学习研究与技术演示，不构成任何投资建议。<br>
                    投资决策请咨询持牌专业机构。作者不对使用本工具产生的任何损失承担责任。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ── Tab 2: 缠论K线图 ─────────────────────────────────────────────────────────

with tab_chanlun:
    render_chanlun_tab()


# ── Helper functions (must be defined before use) ────────────────────────────


def _calc_year_span(start: str, end: str) -> float:
    """计算两个日期之间的年数跨度。"""
    try:
        s = datetime.strptime(str(start)[:10], "%Y-%m-%d")
        e = datetime.strptime(str(end)[:10], "%Y-%m-%d")
        return (e - s).days / 365.25
    except Exception:
        return 0.0


def _render_data_sync_tab() -> None:
    """Render the data sync & cache management tab content."""

    # ── Level / date constants ──
    _LEVEL_OPTIONS = {
        "日线": "daily", "30分钟": "30min", "周线": "weekly", "月线": "monthly",
        "1分钟": "1min", "5分钟": "5min", "15分钟": "15min", "60分钟": "60min",
    }
    _DEFAULT_LEVELS = ["日线", "30分钟"]
    _LEVEL_CN_MAP = {
        "daily": "日线", "weekly": "周线", "monthly": "月线",
        "1min": "1分钟", "5min": "5分钟", "15min": "15分钟",
        "30min": "30分钟", "60min": "60分钟",
    }
    _DEFAULT_START = datetime(2024, 1, 1).date()

    st.markdown(
        """
        <div style="margin-bottom:1rem;">
            <span style="font-size:2rem; font-weight:900; color:#ff5a1f;">💾</span>
            <span style="font-size:1.5rem; font-weight:800; color:#f5f1eb;">数据同步</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    sync_tab1, sync_tab2, sync_tab3, sync_tab4 = st.tabs(
        ["📈 个股同步", "📊 指数同步", "🏭 板块同步", "📂 缓存管理"]
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # Tab 1: 个股同步
    # ═══════════════════════════════════════════════════════════════════════════
    with sync_tab1:
        st.caption("输入股票代码，选择K线级别和起始日期，自动从四大源获取数据")

        col_input, col_date = st.columns([3, 2])
        with col_input:
            sync_input = st.text_input(
                "股票代码",
                placeholder="例: 600519 或 贵州茅台",
                key="sync_input",
            )
        with col_date:
            start_date = st.date_input(
                "起始日期",
                value=_DEFAULT_START,
                key="sync_start_date",
            )

        selected_levels = st.multiselect(
            "K线级别（可多选）",
            options=list(_LEVEL_OPTIONS.keys()),
            default=_DEFAULT_LEVELS,
            key="sync_levels",
        )

        col_len, col_gap = st.columns([1, 3])
        with col_len:
            datalen = st.number_input(
                "K线数量上限",
                min_value=100,
                max_value=10000,
                value=10000,
                step=500,
                key="sync_datalen",
            )

        if st.button("🚀 开始同步", type="primary", use_container_width=True,
                      disabled=not sync_input or not selected_levels):
            resolved_code = None
            try:
                resolved_code = resolve_ticker(sync_input.strip())
                if resolved_code != sync_input.strip():
                    st.info(f"✅ {sync_input.strip()} → {resolved_code}")
            except ValueError as e:
                st.error(f"❌ 无法解析股票代码：{e}")
                resolved_code = None

            if resolved_code:
                for i, lvl_label in enumerate(selected_levels):
                    level = _LEVEL_OPTIONS[lvl_label]
                    # 多级别间加延时，避免连续请求触发限流
                    if i > 0:
                        time.sleep(0.5)
                    with st.spinner(f"同步 {resolved_code} {lvl_label}..."):
                        result = sync_stock_data(
                            resolved_code,
                            level=level,
                            datalen=datalen,
                            start_date=str(start_date) if start_date else None,
                        )
                    if result["status"] == "ok":
                        st.success(
                            f"✅ **{resolved_code}** · {lvl_label} · "
                            f"{result['rows']} 根 · "
                            f"来源:{result['source']} · "
                            f"{result['date_range'][0]} ~ {result['date_range'][1]}"
                        )
                    else:
                        st.error(f"❌ **{resolved_code}** · {lvl_label} · {result.get('error', '未知错误')}")

        # ── 批量同步 ──
        st.markdown("---")
        with st.expander("📋 批量同步", expanded=False):
            batch_input = st.text_area(
                "股票代码列表（逗号分隔）",
                placeholder="例: 600519,000858,300750",
                key="batch_input",
            )
            batch_start = st.date_input(
                "批量起始日期",
                value=_DEFAULT_START,
                key="batch_start_date",
            )
            batch_levels = st.multiselect(
                "批量K线级别",
                options=list(_LEVEL_OPTIONS.keys()),
                default=["日线"],
                key="batch_levels",
            )
            batch_datalen = st.number_input(
                "批量K线数量",
                min_value=100,
                max_value=10000,
                value=10000,
                step=500,
                key="batch_datalen",
            )

            if st.button("批量同步", type="primary", disabled=not batch_input or not batch_levels):
                codes = [c.strip() for c in batch_input.split(",") if c.strip()]
                for raw_code in codes:
                    try:
                        resolved_code = resolve_ticker(raw_code)
                    except ValueError as e:
                        st.error(f"❌ {raw_code}: {e}")
                        continue

                    for lvl_label in batch_levels:
                        batch_level = _LEVEL_OPTIONS[lvl_label]
                        with st.spinner(f"同步 {resolved_code} {lvl_label}..."):
                            result = sync_stock_data(
                                resolved_code,
                                level=batch_level,
                                datalen=batch_datalen,
                                start_date=str(batch_start) if batch_start else None,
                            )
                        if result["status"] == "ok":
                            st.success(
                                f"✅ {resolved_code} · {lvl_label} · "
                                f"{result['rows']}根 · 来源:{result['source']}"
                            )
                        else:
                            st.warning(f"⚠️ {resolved_code} · {lvl_label} · {result.get('error', '未知')}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Tab 2: 指数同步
    # ═══════════════════════════════════════════════════════════════════════════
    with sync_tab2:
        st.caption("同步主要指数K线数据（沪指/深成指/创业板/科创板/北京等）")

        # 预置指数列表
        index_list = get_index_list()

        # 显示五大核心指数
        core_indices = [i for i in index_list if i["code"] in
                        ("000001", "399001", "399006", "000688", "899050")]
        other_indices = [i for i in index_list if i not in core_indices]

        index_names = [f"{i['name']} ({i['code']})" for i in index_list]
        selected_indices = st.multiselect(
            "选择指数（默认五大核心指数）",
            options=index_names,
            default=[f"{i['name']} ({i['code']})" for i in core_indices],
            key="index_select",
        )

        col_id, col_il = st.columns([2, 1])
        with col_id:
            index_start = st.date_input(
                "指数起始日期",
                value=_DEFAULT_START,
                key="index_start_date",
            )
        with col_il:
            index_datalen = st.number_input(
                "K线数量上限",
                min_value=100,
                max_value=10000,
                value=10000,
                step=500,
                key="index_datalen",
            )

        index_levels = st.multiselect(
            "指数K线级别",
            options=list(_LEVEL_OPTIONS.keys()),
            default=["日线", "30分钟"],
            key="index_levels",
        )

        if st.button("🚀 同步指数", type="primary", use_container_width=True,
                      disabled=not selected_indices or not index_levels):
            for i_idx, sel in enumerate(selected_indices):
                code = sel.split("(")[-1].rstrip(")")
                name = sel.split("(")[0].strip()

                for j, lvl_label in enumerate(index_levels):
                    level = _LEVEL_OPTIONS[lvl_label]
                    # 多指数/多级别间加延时
                    if i_idx > 0 or j > 0:
                        time.sleep(0.5)
                    with st.spinner(f"同步 {name} {lvl_label}..."):
                        result = sync_index_data(
                            code,
                            index_name=name,
                            level=level,
                            datalen=index_datalen,
                            start_date=str(index_start) if index_start else None,
                        )
                    if result["status"] == "ok":
                        st.success(
                            f"✅ **{name}** · {lvl_label} · "
                            f"{result['rows']} 根 · "
                            f"来源:{result['source']} · "
                            f"{result['date_range'][0]} ~ {result['date_range'][1]}"
                        )
                    else:
                        st.error(f"❌ **{name}** · {lvl_label} · {result.get('error', '未知')}")

        # 一键同步核心指数
        st.markdown("---")
        with st.expander("⚡ 一键同步五大核心指数", expanded=False):
            st.markdown(" · ".join(f"**{i['name']}** ({i['code']})" for i in core_indices))
            quick_index_levels = st.multiselect(
                "级别",
                options=list(_LEVEL_OPTIONS.keys()),
                default=["日线"],
                key="index_quick_levels",
            )
            if st.button("一键同步核心指数", type="primary", disabled=not quick_index_levels):
                for i_idx, idx in enumerate(core_indices):
                    for j, lvl_label in enumerate(quick_index_levels):
                        level = _LEVEL_OPTIONS[lvl_label]
                        if i_idx > 0 or j > 0:
                            time.sleep(0.5)
                        with st.spinner(f"同步 {idx['name']} {lvl_label}..."):
                            result = sync_index_data(
                                idx["code"],
                                index_name=idx["name"],
                                level=level,
                                datalen=index_datalen,
                                start_date=str(index_start) if index_start else None,
                            )
                        if result["status"] == "ok":
                            st.success(f"✅ {idx['name']} · {lvl_label} · {result['rows']}根")
                        else:
                            st.warning(f"⚠️ {idx['name']} · {lvl_label} · 失败")

    # ═══════════════════════════════════════════════════════════════════════════
    # Tab 3: 板块同步
    # ═══════════════════════════════════════════════════════════════════════════
    with sync_tab3:
        st.caption("从东方财富同步行业/概念板块K线数据到本地缓存")

        sector_type = st.radio(
            "板块类型",
            options=["industry", "concept"],
            format_func=lambda x: "🏭 行业板块" if x == "industry" else "💡 概念板块",
            horizontal=True,
            key="sector_type",
        )

        # Load sector list on demand
        @st.cache_data(ttl=300)
        def _load_sector_list(stype: str):
            return get_sector_list(stype)

        sector_list = _load_sector_list(sector_type)

        if not sector_list:
            st.warning("无法获取板块列表，请检查网络连接")
        else:
            # Sector selection
            sector_names = [f"{s['name']} ({s['code']})" for s in sector_list]
            selected_sectors = st.multiselect(
                f"选择板块（共 {len(sector_list)} 个，支持搜索）",
                options=sector_names,
                key="sector_select",
            )

            col_sd, col_sl = st.columns([2, 1])
            with col_sd:
                sector_start = st.date_input(
                    "板块起始日期",
                    value=_DEFAULT_START,
                    key="sector_start_date",
                )
            with col_sl:
                sector_datalen = st.number_input(
                    "K线数量上限",
                    min_value=100,
                    max_value=10000,
                    value=10000,
                    step=500,
                    key="sector_datalen",
                )

            sector_levels = st.multiselect(
                "板块K线级别",
                options=list(_LEVEL_OPTIONS.keys()),
                default=["日线"],
                key="sector_levels",
            )

            if st.button("🚀 同步板块", type="primary", use_container_width=True,
                          disabled=not selected_sectors or not sector_levels):
                for sel in selected_sectors:
                    # Parse "白酒 (BK0477)" → code=BK0477, name=白酒
                    code = sel.split("(")[-1].rstrip(")")
                    name = sel.split("(")[0].strip()

                    for lvl_label in sector_levels:
                        level = _LEVEL_OPTIONS[lvl_label]
                        with st.spinner(f"同步 {name} {lvl_label}..."):
                            result = sync_sector_data(
                                code,
                                sector_name=name,
                                level=level,
                                datalen=sector_datalen,
                                start_date=str(sector_start) if sector_start else None,
                            )
                        if result["status"] == "ok":
                            st.success(
                                f"✅ **{name}** · {lvl_label} · "
                                f"{result['rows']} 根 · "
                                f"{result['date_range'][0]} ~ {result['date_range'][1]}"
                            )
                        else:
                            st.error(f"❌ **{name}** · {lvl_label} · {result.get('error', '未知')}")

        # Quick sync: top N sectors
        st.markdown("---")
        with st.expander("⚡ 快速同步涨幅前N板块", expanded=False):
            if sector_list:
                top_n = st.slider("涨幅前N", 1, 30, 10, key="sector_top_n")
                top_sectors = sorted(sector_list, key=lambda x: x.get("change_pct", 0), reverse=True)[:top_n]
                st.markdown(
                    " | ".join(
                        f"{s['name']}({s['change_pct']:+.1f}%)"
                        for s in top_sectors
                    )
                )
                quick_levels = st.multiselect(
                    "快速同步级别",
                    options=list(_LEVEL_OPTIONS.keys()),
                    default=["日线"],
                    key="sector_quick_levels",
                )
                if st.button("快速同步", disabled=not quick_levels):
                    for s in top_sectors:
                        for lvl_label in quick_levels:
                            level = _LEVEL_OPTIONS[lvl_label]
                            with st.spinner(f"同步 {s['name']} {lvl_label}..."):
                                result = sync_sector_data(
                                    s["code"],
                                    sector_name=s["name"],
                                    level=level,
                                    datalen=sector_datalen,
                                    start_date=str(sector_start) if sector_start else None,
                                )
                            if result["status"] == "ok":
                                st.success(
                                    f"✅ {s['name']} · {lvl_label} · "
                                    f"{result['rows']}根"
                                )
                            else:
                                st.warning(f"⚠️ {s['name']} · {lvl_label} · 失败")

    # ═══════════════════════════════════════════════════════════════════════════
    # Tab 4: 缓存管理
    # ═══════════════════════════════════════════════════════════════════════════
    with sync_tab4:
        cached_stocks = get_cached_stocks()
        cached_indices = get_cached_indices()
        cached_sectors = get_cached_sectors()

        if not cached_stocks and not cached_indices and not cached_sectors:
            st.info("暂无缓存数据。请先同步股票、指数或板块数据。")
        else:
            # ── Stock cache ──
            if cached_stocks:
                st.markdown(f"#### 📈 个股缓存（{len(cached_stocks)} 条）")
                for item in cached_stocks:
                    code = item.get("code", "?")
                    level_val = item.get("level", "daily")
                    rows = item.get("rows", 0)
                    date_range = item.get("date_range", [])
                    last_sync = item.get("last_sync", "未知")
                    source = item.get("source", "未知")
                    start = item.get("start_date", "")
                    level_cn = _LEVEL_CN_MAP.get(level_val, level_val)

                    col_info, col_action = st.columns([5, 1])
                    with col_info:
                        parts = [f"**{code}** · {level_cn} · {rows} 根"]
                        if date_range and len(date_range) == 2:
                            year_span = _calc_year_span(date_range[0], date_range[1])
                            parts.append(f"{date_range[0]} ~ {date_range[1]} ({year_span:.1f}年)")
                        if source:
                            parts.append(f"来源:{source}")
                        if start:
                            parts.append(f"起始:{start}")
                        parts.append(f"同步:{last_sync}")
                        st.markdown(" · ".join(parts))
                    with col_action:
                        if st.button("🗑️", key=f"del_{code}_{level_val}",
                                      help=f"删除 {code} {level_cn} 缓存"):
                            if delete_cache(code, level=level_val):
                                st.success(f"已删除 {code} {level_cn}缓存")
                                st.rerun()
                            else:
                                st.warning(f"未找到 {code} {level_val} 的缓存文件")

            # ── Index cache ──
            if cached_indices:
                st.markdown(f"#### 📊 指数缓存（{len(cached_indices)} 条）")
                for item in cached_indices:
                    code = item.get("code", "?")
                    name = item.get("name", "")
                    level_val = item.get("level", "daily")
                    rows = item.get("rows", 0)
                    date_range = item.get("date_range", [])
                    last_sync = item.get("last_sync", "未知")
                    source = item.get("source", "未知")
                    level_cn = _LEVEL_CN_MAP.get(level_val, level_val)

                    col_info, col_action = st.columns([5, 1])
                    with col_info:
                        label = f"**{name or code}**"
                        parts = [f"{label} · {level_cn} · {rows} 根"]
                        if date_range and len(date_range) == 2:
                            year_span = _calc_year_span(date_range[0], date_range[1])
                            parts.append(f"{date_range[0]} ~ {date_range[1]} ({year_span:.1f}年)")
                        if source:
                            parts.append(f"来源:{source}")
                        parts.append(f"同步:{last_sync}")
                        st.markdown(" · ".join(parts))
                    with col_action:
                        if st.button("🗑️", key=f"del_i_{code}_{level_val}",
                                      help=f"删除 {name or code} {level_cn} 缓存"):
                            if delete_index_cache(code, level=level_val):
                                st.success(f"已删除 {name or code} {level_cn}缓存")
                                st.rerun()
                            else:
                                st.warning(f"未找到 {code} {level_val} 的指数缓存")

            # ── Sector cache ──
            if cached_sectors:
                st.markdown(f"#### 🏭 板块缓存（{len(cached_sectors)} 条）")
                for item in cached_sectors:
                    code = item.get("code", "?")
                    name = item.get("name", "")
                    level_val = item.get("level", "daily")
                    rows = item.get("rows", 0)
                    date_range = item.get("date_range", [])
                    last_sync = item.get("last_sync", "未知")
                    level_cn = _LEVEL_CN_MAP.get(level_val, level_val)

                    col_info, col_action = st.columns([5, 1])
                    with col_info:
                        label = f"**{name or code}**" if name else f"**{code}**"
                        parts = [f"{label} · {level_cn} · {rows} 根"]
                        if date_range and len(date_range) == 2:
                            parts.append(f"{date_range[0]} ~ {date_range[1]}")
                        parts.append(f"同步:{last_sync}")
                        st.markdown(" · ".join(parts))
                    with col_action:
                        if st.button("🗑️", key=f"del_s_{code}_{level_val}",
                                      help=f"删除 {name or code} {level_cn} 缓存"):
                            if delete_sector_cache(code, level=level_val):
                                st.success(f"已删除 {name or code} {level_cn}缓存")
                                st.rerun()
                            else:
                                st.warning(f"未找到 {code} {level_val} 的板块缓存")


# ── Tab 2: 数据同步 ──────────────────────────────────────────────────────────

with tab_datasync:
    _render_data_sync_tab()
