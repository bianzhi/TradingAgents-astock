"""Sidebar: stock input, LLM config, and history list."""

from __future__ import annotations

from datetime import date

import streamlit as st

from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS
from web.history import get_history

# Provider display names in recommended order
_PROVIDERS: list[tuple[str, str]] = [
    ("MiniMax（推荐·国内直连）", "minimax"),
    ("DeepSeek", "deepseek"),
    ("通义千问 Qwen", "qwen"),
    ("智谱 GLM", "glm"),
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Google Gemini", "google"),
    ("xAI Grok", "xai"),
    ("Ollama（本地）", "ollama"),
]

_PROVIDER_DISPLAY = [name for name, _ in _PROVIDERS]
_PROVIDER_KEYS = [key for _, key in _PROVIDERS]


def _resolve_user_input(raw: str) -> tuple[str, str | None]:
    """Resolve raw user input to (ticker_code, error_msg).

    Accepts 6-digit codes or Chinese stock names (e.g. '宝光股份').
    Returns (code, None) on success or ("", error_msg) on failure.
    """
    from tradingagents.dataflows.a_stock import resolve_ticker

    try:
        code = resolve_ticker(raw)
        return code, None
    except ValueError as e:
        return "", str(e)


def _render_llm_config() -> None:
    """Render LLM provider and model selection controls.

    Model choices are persisted to URL query params so they survive
    browser refresh (F5 / Cmd+R).
    """

    # --- On first load after browser refresh, restore from URL query params ---
    if "llm_provider" not in st.session_state:
        params = st.query_params
        provider = params.get("provider")
        if provider and provider in _PROVIDER_KEYS:
            st.session_state["llm_provider_idx"] = _PROVIDER_KEYS.index(provider)
        quick = params.get("quick_model")
        if quick:
            st.session_state["_restore_quick"] = quick
        deep = params.get("deep_model")
        if deep:
            st.session_state["_restore_deep"] = deep

    # --- Provider ---
    provider_idx = st.selectbox(
        "LLM 供应商",
        range(len(_PROVIDERS)),
        format_func=lambda i: _PROVIDER_DISPLAY[i],
        key="llm_provider_idx",
        help="选择你配置了 API Key 的供应商",
    )
    provider_key = _PROVIDER_KEYS[provider_idx]
    st.session_state["llm_provider"] = provider_key

    quick_val = ""
    deep_val = ""

    if provider_key in MODEL_OPTIONS:
        quick_options = MODEL_OPTIONS[provider_key]["quick"]
        deep_options = MODEL_OPTIONS[provider_key]["deep"]

        quick_labels = [label for label, _ in quick_options]
        quick_values = [value for _, value in quick_options]
        deep_labels = [label for label, _ in deep_options]
        deep_values = [value for _, value in deep_options]

        # --- Quick model ---
        restore_quick = st.session_state.pop("_restore_quick", None)
        if restore_quick and restore_quick in quick_values:
            st.session_state["quick_model_idx"] = quick_values.index(restore_quick)

        quick_idx = st.selectbox(
            "快速思考模型",
            range(len(quick_options)),
            format_func=lambda i: quick_labels[i],
            key="quick_model_idx",
            help="用于常规分析任务，速度优先",
        )
        quick_val = quick_values[quick_idx]

        # --- Deep model ---
        restore_deep = st.session_state.pop("_restore_deep", None)
        if restore_deep and restore_deep in deep_values:
            st.session_state["deep_model_idx"] = deep_values.index(restore_deep)

        deep_idx = st.selectbox(
            "深度思考模型",
            range(len(deep_options)),
            format_func=lambda i: deep_labels[i],
            key="deep_model_idx",
            help="用于辩论/决策等需要深度推理的任务",
        )
        deep_val = deep_values[deep_idx]
    else:
        # Custom provider — restore previous text values
        restore_quick = st.session_state.pop("_restore_quick", None)
        restore_deep = st.session_state.pop("_restore_deep", None)
        quick_val = st.text_input(
            "快速思考模型 ID",
            value=restore_quick or "",
            key="custom_quick_model",
        )
        deep_val = st.text_input(
            "深度思考模型 ID",
            value=restore_deep or "",
            key="custom_deep_model",
        )

    st.session_state["quick_think_llm"] = quick_val
    st.session_state["deep_think_llm"] = deep_val

    # --- Persist to URL query params so choices survive browser refresh ---
    if (
        st.query_params.get("provider") != provider_key
        or st.query_params.get("quick_model") != quick_val
        or st.query_params.get("deep_model") != deep_val
    ):
        st.query_params["provider"] = provider_key
        st.query_params["quick_model"] = quick_val
        st.query_params["deep_model"] = deep_val


def render_sidebar() -> None:
    """Render the sidebar with input controls and history."""

    st.markdown(
        """
        <div style="text-align:center; margin-bottom:1.5rem;">
            <span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Trading</span><span style="font-size:2rem; font-weight:800; color:#f5f1eb;">Agents</span><span style="font-size:2rem; font-weight:800; color:#f5f1eb;">-</span><span style="font-size:2rem; font-weight:800; color:#ff5a1f;">Astock</span>
            <div style="font-size:0.85rem; color:#888; margin-top:0.2rem;">
                A股多Agent投研系统
            </div>
            <div style="font-size:0.7rem; color:#555; margin-top:0.3rem;">
                by <a href="https://github.com/simonlin1212" style="color:#ff5a1f; text-decoration:none;">simonlin1212</a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown("---")
    st.markdown("#### 新建分析")

    ticker = st.text_input(
        "股票搜索",
        placeholder="输入代码、中文名或拼音首字母（如: 宁德、300750、ndsd）",
        key="input_ticker",
        help="支持6位代码、中文股票名、拼音首字母（如 ndsd=宁德时代, gzmt=贵州茅台）",
    )

    # --- Live search dropdown ---
    if ticker and ticker.strip():
        from tradingagents.dataflows.a_stock import search_stocks

        results = search_stocks(ticker.strip(), limit=10)
        if len(results) == 1:
            # Single exact match — show confirmation
            r = results[0]
            st.caption(f"🔍 {r['code']} {r['name']}")
        elif len(results) > 1:
            # Multiple matches — show as radio select
            options = {f"{r['code']}  {r['name']}": r["code"] for r in results}
            st.caption(f"🔍 找到 {len(results)} 个匹配，点击选择:")
            selected = st.radio(
                "匹配结果",
                list(options.keys()),
                label_visibility="collapsed",
                key="search_result_radio",
            )
            if selected:
                ticker = options[selected]
                # Update the text input with the selected ticker
                st.session_state["input_ticker"] = ticker
                st.rerun()
        elif len(ticker.strip()) >= 2:
            st.caption("未找到匹配结果")

    trade_date = st.date_input(
        "分析日期",
        value=date.today(),
        key="input_date",
    )

    with st.expander("⚙️ 模型配置", expanded=False):
        _render_llm_config()

    tracker = st.session_state.get("tracker")
    is_busy = tracker is not None and tracker.is_running

    if st.button(
        "开始分析" if not is_busy else "分析进行中...",
        use_container_width=True,
        disabled=is_busy or not ticker,
        type="primary",
    ):
        resolved_code, err = _resolve_user_input(ticker)
        if err:
            st.error(f"❌ {err}")
        else:
            if resolved_code != ticker.strip():
                st.success(f"✅ {ticker.strip()} → {resolved_code}")
            st.session_state["start_analysis"] = {
                "ticker": resolved_code,
                "trade_date": trade_date.strftime("%Y-%m-%d"),
            }
            st.session_state["viewing_history"] = None

    st.markdown("---")
    st.markdown("#### 历史记录")

    history = get_history()
    if not history:
        st.caption("暂无历史记录")
        return

    for entry in history[:20]:
        t, d = entry["ticker"], entry["date"]
        label = f"{t}  ·  {d}"
        if st.button(label, key=f"hist_{t}_{d}", use_container_width=True):
            st.session_state["viewing_history"] = entry["path"]
            st.session_state["start_analysis"] = None

    st.markdown("---")
    st.caption("⚠️ 仅供学习研究，不构成投资建议")
