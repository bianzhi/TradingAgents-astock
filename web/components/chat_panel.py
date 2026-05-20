"""Chat panel for user feedback and supplementary opinions during/after analysis."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st


def _init_chat_state(ticker: str, trade_date: str) -> None:
    """Ensure chat messages list exists in session state."""
    key = f"chat_msgs_{ticker}_{trade_date}"
    if key not in st.session_state:
        st.session_state[key] = []


def _get_chat_messages(ticker: str, trade_date: str) -> list[dict]:
    """Get chat messages for a specific analysis."""
    key = f"chat_msgs_{ticker}_{trade_date}"
    return st.session_state.get(key, [])


def _add_chat_message(ticker: str, trade_date: str, role: str, content: str) -> None:
    """Add a message to the chat history."""
    key = f"chat_msgs_{ticker}_{trade_date}"
    if key not in st.session_state:
        st.session_state[key] = []
    st.session_state[key].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().strftime("%H:%M:%S"),
    })


def render_chat(ticker: str, trade_date: str) -> None:
    """Render the chat panel for supplementary opinions and feedback.

    This is a local chat — messages are stored in Streamlit session state.
    For integration with the agent pipeline, messages can be injected
    into the analysis via the 'user_feedback' mechanism.
    """
    _init_chat_state(ticker, trade_date)

    messages = _get_chat_messages(ticker, trade_date)

    # Chat header
    st.markdown(
        """
        <div style="
            display: flex;
            align-items: center;
            gap: 0.5rem;
            margin-bottom: 0.5rem;
        ">
            <span style="font-size: 1.1rem;">💬</span>
            <span style="font-size: 1rem; font-weight: 700; color: #f5f1eb;">
                分析对话
            </span>
            <span style="font-size: 0.75rem; color: #666; margin-left: auto;">
                补充意见 · 提问 · 反馈
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Chat messages area
    chat_container = st.container()

    with chat_container:
        if not messages:
            st.markdown(
                '<div style="text-align:center; color:#555; font-size:0.85rem; padding:1rem 0;">'
                '暂无消息，输入你的意见或问题 👇'
                '</div>',
                unsafe_allow_html=True,
            )
        else:
            for msg in messages:
                role = msg["role"]
                content = msg["content"]
                ts = msg["timestamp"]

                if role == "user":
                    st.markdown(
                        f"""
                        <div style="
                            display: flex;
                            justify-content: flex-end;
                            margin: 0.3rem 0;
                        ">
                            <div style="
                                background: #1a2e1a;
                                border: 1px solid #2a4a2a;
                                border-radius: 12px 12px 2px 12px;
                                padding: 0.5rem 0.8rem;
                                max-width: 80%;
                            ">
                                <div style="color: #a5d6a7; font-size: 0.85rem;">
                                    {content}
                                </div>
                                <div style="color: #555; font-size: 0.65rem; text-align: right; margin-top: 0.2rem;">
                                    {ts}
                                </div>
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"""
                        <div style="
                            display: flex;
                            justify-content: flex-start;
                            margin: 0.3rem 0;
                        ">
                            <div style="
                                background: #1a1a2e;
                                border: 1px solid #2a2a4a;
                                border-radius: 12px 12px 12px 2px;
                                padding: 0.5rem 0.8rem;
                                max-width: 80%;
                            ">
                                <div style="color: #90caf9; font-size: 0.85rem;">
                                    {content}
                                </div>
                                <div style="color: #555; font-size: 0.65rem; margin-top: 0.2rem;">
                                    🤖 Agent · {ts}
                                </div>
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

    # Input area
    col_input, col_send = st.columns([5, 1])

    with col_input:
        user_input = st.text_input(
            "消息",
            placeholder="输入补充意见、问题或反馈...",
            key=f"chat_input_{ticker}_{trade_date}",
            label_visibility="collapsed",
        )

    with col_send:
        send_clicked = st.button(
            "发送",
            key=f"chat_send_{ticker}_{trade_date}",
            use_container_width=True,
            type="primary",
        )

    if send_clicked and user_input and user_input.strip():
        _add_chat_message(ticker, trade_date, "user", user_input.strip())

        # Generate agent acknowledgment
        feedback = user_input.strip()
        ack = _generate_acknowledgment(feedback)
        _add_chat_message(ticker, trade_date, "assistant", ack)

        st.rerun()


# ── Predefined quick feedback buttons ─────────────────────────────────────────

def render_quick_feedback(ticker: str, trade_date: str) -> None:
    """Render quick feedback buttons for common opinions."""
    _init_chat_state(ticker, trade_date)

    st.markdown(
        '<div style="font-size:0.8rem; color:#888; margin-bottom:0.3rem;">快速反馈</div>',
        unsafe_allow_html=True,
    )

    quick_options = [
        ("🔍 数据遗漏", "我发现分析中有数据遗漏，请补充检查"),
        ("⚠️ 逻辑质疑", "报告中某段分析逻辑我有不同看法"),
        ("📋 补充信息", "我有一些补充信息需要纳入分析"),
        ("❓ 提问", "对报告内容有疑问需要解释"),
    ]

    cols = st.columns(len(quick_options))
    for col, (label, message) in zip(cols, quick_options):
        if col.button(
            label,
            key=f"quick_fb_{label}_{ticker}_{trade_date}",
            use_container_width=True,
        ):
            _add_chat_message(ticker, trade_date, "user", message)
            ack = _generate_acknowledgment(message)
            _add_chat_message(ticker, trade_date, "assistant", ack)
            st.rerun()


# ── Acknowledgment generator ──────────────────────────────────────────────────

def _generate_acknowledgment(feedback: str) -> str:
    """Generate a simple acknowledgment for user feedback.

    This is a heuristic acknowledgment — not an LLM call.
    For full agent integration, the feedback would be injected into
    the analysis pipeline.
    """
    if "遗漏" in feedback or "缺失" in feedback:
        return "收到！已记录数据遗漏反馈。质量门控环节会检查并自动修复缺失数据，修复结果将在报告中标注。"
    if "逻辑" in feedback or "质疑" in feedback or "不同" in feedback:
        return "收到！已记录逻辑质疑。辩论环节的多空对抗机制会检验报告逻辑的稳健性。如有具体论据，欢迎进一步补充。"
    if "补充" in feedback:
        return "收到补充信息！已记录。后续分析师可以参考此信息进行修正。请提供更多细节以便纳入分析。"
    if "疑问" in feedback or "提问" in feedback:
        return "收到问题！请具体说明哪部分需要解释，我会尽量提供更清晰的说明。"
    return "收到反馈！已记录。感谢你的补充意见，后续分析会参考此信息。"


# ── Export chat for pipeline injection ─────────────────────────────────────────

def get_user_feedback_text(ticker: str, trade_date: str) -> str:
    """Export all user chat messages as a single text block for pipeline injection."""
    messages = _get_chat_messages(ticker, trade_date)
    user_msgs = [m for m in messages if m["role"] == "user"]
    if not user_msgs:
        return ""
    lines = [f"[{m['timestamp']}] {m['content']}" for m in user_msgs]
    return "用户反馈:\n" + "\n".join(lines)
