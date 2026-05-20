"""Background thread runner for TradingAgentsGraph pipeline."""

from __future__ import annotations

import re
import threading
from typing import Any

from web.progress import PIPELINE_STAGES, ProgressTracker


_REPORT_KEY_TO_STAGE = {s["report_key"]: s["id"] for s in PIPELINE_STAGES}
_STAGE_TO_REPORT_KEY = {s["id"]: s["report_key"] for s in PIPELINE_STAGES}

_ANALYST_REPORT_KEYS = [
    "market_report", "sentiment_report", "news_report",
    "fundamentals_report", "policy_report", "hot_money_report", "lockup_report",
]


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _detect_completed_stages(
    chunk: dict[str, Any],
    tracker: ProgressTracker,
) -> None:
    """Check the streamed chunk for newly completed stages."""
    for report_key in _ANALYST_REPORT_KEYS:
        stage_id = _REPORT_KEY_TO_STAGE[report_key]
        content = chunk.get(report_key, "")
        if content and tracker.stage_status(stage_id) != "done":
            tracker.mark_stage_done(stage_id, _strip_think_tags(str(content)))

    dqs = chunk.get("data_quality_summary", "")
    if dqs and tracker.stage_status("quality_gate") != "done":
        tracker.mark_stage_done("quality_gate", str(dqs))

    qrl = chunk.get("quality_repair_log", "")
    if qrl:
        tracker.repair_log = str(qrl)

    debate = chunk.get("investment_debate_state")
    if debate and isinstance(debate, dict):
        judge = debate.get("judge_decision", "")
        if judge and tracker.stage_status("debate") != "done":
            tracker.mark_stage_done("debate", str(judge))

    trader_plan = chunk.get("trader_investment_plan", "")
    if trader_plan and tracker.stage_status("trader") != "done":
        tracker.mark_stage_done("trader", _strip_think_tags(str(trader_plan)))

    risk = chunk.get("risk_debate_state")
    if risk and isinstance(risk, dict):
        risk_judge = risk.get("judge_decision", "")
        if risk_judge and tracker.stage_status("risk") != "done":
            tracker.mark_stage_done("risk", str(risk_judge))

    final = chunk.get("final_trade_decision", "")
    if final and tracker.stage_status("pm") != "done":
        tracker.mark_stage_done("pm", _strip_think_tags(str(final)))


def _infer_active_stage(tracker: ProgressTracker) -> None:
    """Set the current_stage to the first non-completed stage."""
    from web.progress import STAGE_IDS
    for sid in STAGE_IDS:
        if tracker.stage_status(sid) == "pending":
            tracker.mark_stage_active(sid)
            return


def _run(ticker: str, trade_date: str, config: dict, tracker: ProgressTracker) -> None:
    """Execute the full pipeline in the current thread."""
    from cli.stats_handler import StatsCallbackHandler
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    stats = StatsCallbackHandler()

    config["checkpoint_enabled"] = True

    graph = TradingAgentsGraph(
        debug=True,
        config=config,
        callbacks=[stats],
    )

    init_state = graph.propagator.create_initial_state(ticker, trade_date)
    args = graph.propagator.get_graph_args(callbacks=[stats])

    last_chunk: dict[str, Any] = {}

    for chunk in graph.graph.stream(init_state, **args):
        last_chunk = chunk
        _detect_completed_stages(chunk, tracker)
        _infer_active_stage(tracker)

        s = stats.get_stats()
        tracker.update_stats(s["llm_calls"], s["tool_calls"], s["tokens_in"], s["tokens_out"])

    # Build partial_state with report_key (not stage_id) as dict keys,
    # so it's compatible with render_report / generate_pdf.
    def _build_partial_state() -> dict[str, Any]:
        partial: dict[str, Any] = {}
        for stage_id, content in tracker.stage_reports.items():
            if content:
                report_key = _STAGE_TO_REPORT_KEY.get(stage_id, stage_id)
                partial[report_key] = content
        if tracker.repair_log:
            partial["quality_repair_log"] = tracker.repair_log
        # Merge in any raw state keys from last_chunk (for debate/risk dict etc.)
        if last_chunk:
            for k in ("investment_debate_state", "risk_debate_state",
                      "investment_plan", "trader_investment_plan",
                      "final_trade_decision", "data_quality_summary"):
                v = last_chunk.get(k)
                if v and k not in partial:
                    partial[k] = v
        return partial

    try:
        signal = graph.process_signal(last_chunk.get("final_trade_decision", ""))

        graph.ticker = ticker
        graph._log_state(trade_date, last_chunk)

        tracker.mark_complete(last_chunk, signal)
    except Exception:
        raise


def run_analysis_in_thread(
    ticker: str,
    trade_date: str,
    config: dict,
    tracker: ProgressTracker,
) -> threading.Thread:
    """Launch the pipeline in a daemon thread. Returns the thread handle."""
    tracker.ticker = ticker
    tracker.trade_date = trade_date
    tracker.is_running = True
    tracker.mark_stage_active("market")

    def _target() -> None:
        try:
            _run(ticker, trade_date, config, tracker)
        except Exception as exc:
            # Build partial_state using the same mapping as _build_partial_state,
            # but we can't access the closure's last_chunk here — fall back to
            # stage_reports with report_key mapping.
            partial_state: dict[str, Any] = {}
            for stage_id, content in tracker.stage_reports.items():
                if content:
                    report_key = _STAGE_TO_REPORT_KEY.get(stage_id, stage_id)
                    partial_state[report_key] = content
            if tracker.repair_log:
                partial_state["quality_repair_log"] = tracker.repair_log
            tracker.mark_error(str(exc), partial_state=partial_state)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t
