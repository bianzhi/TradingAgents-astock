"""Data quality gate with auto-repair.

Sits between the last analyst Msg Clear and Bull Researcher.
Layer 1: hard checks (code).
Layer 2: LLM review (one call).
Layer 3: auto-repair — for reports graded C or below, re-invoke the analyst
         with a targeted repair prompt (up to max_repair_attempts times).
         If still failing after retries, mark with ⚠️.
"""

from __future__ import annotations

import importlib
import re
from typing import Any

REPORT_FIELDS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
    "policy": "policy_report",
    "hot_money": "hot_money_report",
    "lockup": "lockup_report",
}

ANALYST_NAMES = {
    "market": "技术分析师",
    "social": "情绪分析师",
    "news": "新闻分析师",
    "fundamentals": "基本面分析师",
    "policy": "政策分析师",
    "hot_money": "游资追踪师",
    "lockup": "解禁监控师",
}

# Map analyst type → (module_path, factory_name, llm_arg_name)
ANALYST_FACTORIES = {
    "market": ("tradingagents.agents.analysts.market_analyst", "create_market_analyst"),
    "social": ("tradingagents.agents.analysts.social_media_analyst", "create_social_media_analyst"),
    "news": ("tradingagents.agents.analysts.news_analyst", "create_news_analyst"),
    "fundamentals": ("tradingagents.agents.analysts.fundamentals_analyst", "create_fundamentals_analyst"),
    "policy": ("tradingagents.agents.analysts.policy_analyst", "create_policy_analyst"),
    "hot_money": ("tradingagents.agents.analysts.hot_money_tracker", "create_hot_money_tracker"),
    "lockup": ("tradingagents.agents.analysts.lockup_watcher", "create_lockup_watcher"),
}

MIN_REPORT_LENGTH = 200

FAILURE_MARKERS = [
    "无法获取",
    "I cannot retrieve",
    "I don't have access",
    "unable to fetch",
    "工具调用失败",
]

MAX_REPAIR_ATTEMPTS = 3

# Grade ordering: A=0 (best) → F=4 (worst)
_GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}


def _grade_improved(new_grade: str, old_grade: str) -> bool:
    """Return True if new_grade represents an improvement over old_grade."""
    return _GRADE_ORDER.get(new_grade, 4) < _GRADE_ORDER.get(old_grade, 4)

# ── Hard check ────────────────────────────────────────────────────────────────

def _hard_check_report(analyst_type: str, report: str) -> tuple:
    """Run hard checks on a single report. Returns (grade, detail)."""
    if not report or not report.strip():
        return ("F", "报告为空")

    length = len(report.strip())
    if length < MIN_REPORT_LENGTH:
        return ("D", f"报告过短 ({length} chars < {MIN_REPORT_LENGTH})")

    failure_count = sum(1 for m in FAILURE_MARKERS if m in report)
    stripped = report
    for m in FAILURE_MARKERS:
        stripped = stripped.replace(m, "")
    if failure_count > 0 and len(stripped.strip()) < MIN_REPORT_LENGTH:
        return ("D", f"报告主要由失败信息构成 ({failure_count} 处)")

    has_table = "|" in report and "---" in report
    missing_count = report.count("[数据缺失")

    issues = []
    if not has_table:
        issues.append("缺少汇总表格")
    if missing_count > 0:
        issues.append(f"{missing_count} 处数据缺失")

    if missing_count >= 3:
        return ("C", "；".join(issues))
    if not has_table or missing_count > 0:
        return ("B", "；".join(issues) if issues else "基本合格")

    return ("A", f"完整 ({length} chars)")


# ── Repair helpers ────────────────────────────────────────────────────────────

def _extract_missing_items(report: str) -> list[str]:
    """Extract [数据缺失: xxx] markers from a report."""
    return re.findall(r"\[数据缺失[:：]\s*([^\]]+)\]", report)


def _build_repair_prompt(
    analyst_type: str,
    original_report: str,
    missing_items: list[str],
    grade_detail: str,
    attempt: int,
) -> str:
    """Build a targeted repair prompt for a failing analyst report."""
    name = ANALYST_NAMES[analyst_type]
    items_str = "、".join(f"「{item}」" for item in missing_items) if missing_items else "详见原报告标注"

    return (
        f"你之前作为{name}生成的报告存在质量问题，需要修复。\n\n"
        f"**质量检查结果**: {grade_detail}\n\n"
        f"**需要修复的缺失数据项**: {items_str}\n\n"
        f"**原始报告**:\n---\n{original_report}\n---\n\n"
        f"请重新调用相关工具获取缺失数据，生成**完整的**分析报告。"
        f"如果某项数据确实无法获取，使用 [数据缺失: xxx] 标注并说明原因。\n"
        f"这是第 {attempt} 次修复尝试（最多 {MAX_REPAIR_ATTEMPTS} 次）。"
        f"请务必确保所有能获取的数据都已获取，报告结构完整，包含汇总表格。"
    )


def _invoke_analyst_repair(
    analyst_type: str,
    state: dict,
    llm: Any,
    repair_prompt: str,
) -> str:
    """Re-invoke an analyst with a repair prompt, return the new report."""
    module_path, factory_name = ANALYST_FACTORIES[analyst_type]
    module = importlib.import_module(module_path)
    factory = getattr(module, factory_name)
    analyst_node = factory(llm)

    # Build a minimal state with the repair prompt as the user message
    from langchain_core.messages import HumanMessage
    repair_state = {
        **state,
        "messages": [HumanMessage(content=repair_prompt)],
    }

    result = analyst_node(repair_state)
    report_key = REPORT_FIELDS[analyst_type]
    new_report = result.get(report_key, "")

    # If the analyst didn't produce a report (tool calls pending),
    # we need to run the tool node and then re-invoke.
    if not new_report and result.get("messages"):
        from langchain_core.messages import ToolMessage
        messages = result["messages"]
        last_msg = messages[-1] if messages else None
        if last_msg and hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            # Execute tool calls via the routed vendor functions
            from tradingagents.dataflows.interface import route_to_vendor
            tool_results = []
            for tc in last_msg.tool_calls:
                try:
                    output = route_to_vendor(tc["name"], **tc["args"])
                    tool_results.append(
                        ToolMessage(
                            content=str(output),
                            tool_call_id=tc["id"],
                        )
                    )
                except Exception as e:
                    tool_results.append(
                        ToolMessage(
                            content=f"工具调用失败: {e}",
                            tool_call_id=tc["id"],
                        )
                    )

            # Re-invoke analyst with tool results
            repair_state2 = {
                **state,
                "messages": [HumanMessage(content=repair_prompt), last_msg] + tool_results,
            }
            result2 = analyst_node(repair_state2)
            new_report = result2.get(report_key, "")

            # If still tool calls, one more round
            if not new_report and result2.get("messages"):
                last_msg2 = result2["messages"][-1]
                if hasattr(last_msg2, "tool_calls") and last_msg2.tool_calls:
                    tool_results2 = []
                    for tc in last_msg2.tool_calls:
                        try:
                            output = route_to_vendor(tc["name"], **tc["args"])
                            tool_results2.append(
                                ToolMessage(
                                    content=str(output),
                                    tool_call_id=tc["id"],
                                )
                            )
                        except Exception as e:
                            tool_results2.append(
                                ToolMessage(
                                    content=f"工具调用失败: {e}",
                                    tool_call_id=tc["id"],
                                )
                            )

                    repair_state3 = {
                        **state,
                        "messages": [HumanMessage(content=repair_prompt), last_msg, *tool_results, last_msg2, *tool_results2],
                    }
                    result3 = analyst_node(repair_state3)
                    new_report = result3.get(report_key, "")

    return new_report


# ── LLM review ────────────────────────────────────────────────────────────────

def _build_review_prompt(
    reports: dict, trade_date: str, ticker: str
) -> str:
    """Build the LLM review prompt."""
    report_sections = []
    for analyst_type, field in REPORT_FIELDS.items():
        name = ANALYST_NAMES[analyst_type]
        content = reports.get(field, "（未运行）")
        if not content:
            content = "（报告为空）"
        if len(content) > 3000:
            content = content[:3000] + "\n... (truncated for review)"
        report_sections.append(f"### {name} ({analyst_type})\n{content}")

    all_reports = "\n\n".join(report_sections)

    return f"""你是数据质量审核员。以下是 7 位分析师对 {ticker} 在 {trade_date} 的研究报告。请逐一审核。

{all_reports}

---

请按以下格式输出审核结果（不要输出其他内容）：

## 数据质量审核报告

**标的**: {ticker} | **日期**: {trade_date}

| 分析师 | 评级 | 数据时效 | 缺失项 | 备注 |
|--------|------|----------|--------|------|
| 技术分析师 | A/B/C/D/F | 是否匹配交易日 | 列出缺失的必采项 | 简要说明 |
| 情绪分析师 | ... | ... | ... | ... |
| 新闻分析师 | ... | ... | ... | ... |
| 基本面分析师 | ... | ... | ... | ... |
| 政策分析师 | ... | ... | ... | ... |
| 游资追踪师 | ... | ... | ... | ... |
| 解禁监控师 | ... | ... | ... | ... |

**整体评级**: A/B/C/D/F
**数据可信度**: 高/中/低
**建议**: （如有数据缺失，提醒辩论阶段谨慎使用该报告）

评级标准：
- A: 必采清单全部覆盖，数据时效匹配，有汇总表格
- B: 缺少 1-2 项非关键数据，整体可用
- C: 缺少 3+ 项或有数据时效问题，需谨慎使用
- D: 大量缺失或主要为失败信息，可信度低
- F: 报告为空或完全无效
"""


# ── Quality gate node ─────────────────────────────────────────────────────────

def create_quality_gate(llm, max_repair_attempts: int = MAX_REPAIR_ATTEMPTS):
    """Factory for the data quality gate node.

    Sits between the last analyst Msg Clear and Bull Researcher.
    Layer 1: hard checks (code).
    Layer 2: auto-repair for C/D/F grades (up to max_repair_attempts).
    Layer 3: LLM review (one call).
    Writes data_quality_summary and quality_repair_log to state.
    """

    def quality_gate_node(state) -> dict:
        trade_date = state["trade_date"]
        ticker = state["company_of_interest"]

        reports = {}
        for analyst_type, field in REPORT_FIELDS.items():
            reports[field] = state.get(field, "")

        # ── Layer 1: Hard checks ──────────────────────────────────────────
        hard_results = {}
        for analyst_type, field in REPORT_FIELDS.items():
            grade, detail = _hard_check_report(analyst_type, reports[field])
            hard_results[analyst_type] = (grade, detail)

        # ── Layer 2: Auto-repair ──────────────────────────────────────────
        repair_log: list[dict[str, Any]] = []
        needs_repair = [
            (at, g, d) for at, (g, d) in hard_results.items()
            if g in ("C", "D", "F")
        ]

        for analyst_type, orig_grade, orig_detail in needs_repair:
            current_report = reports[REPORT_FIELDS[analyst_type]]
            repaired = False
            final_grade = orig_grade
            final_detail = orig_detail

            for attempt in range(1, max_repair_attempts + 1):
                missing_items = _extract_missing_items(current_report)
                repair_prompt = _build_repair_prompt(
                    analyst_type, current_report, missing_items,
                    f"[{orig_grade}] {orig_detail}", attempt,
                )

                try:
                    new_report = _invoke_analyst_repair(
                        analyst_type, state, llm, repair_prompt,
                    )
                except Exception as e:
                    repair_log.append({
                        "analyst": analyst_type,
                        "name": ANALYST_NAMES[analyst_type],
                        "attempt": attempt,
                        "status": "error",
                        "error": str(e),
                        "grade_before": orig_grade,
                        "grade_after": orig_grade,
                    })
                    continue

                if not new_report or not new_report.strip():
                    repair_log.append({
                        "analyst": analyst_type,
                        "name": ANALYST_NAMES[analyst_type],
                        "attempt": attempt,
                        "status": "empty",
                        "grade_before": orig_grade,
                        "grade_after": orig_grade,
                    })
                    continue

                # Check the repaired report
                new_grade, new_detail = _hard_check_report(analyst_type, new_report)

                repair_log.append({
                    "analyst": analyst_type,
                    "name": ANALYST_NAMES[analyst_type],
                    "attempt": attempt,
                    "status": "repaired" if _grade_improved(new_grade, orig_grade) or new_grade in ("A", "B") else "still_failing",
                    "grade_before": orig_grade,
                    "grade_after": new_grade,
                    "detail_after": new_detail,
                })

                # Always use the improved report (even if still not perfect)
                if _grade_improved(new_grade, orig_grade) or len(new_report) > len(current_report):
                    current_report = new_report
                    reports[REPORT_FIELDS[analyst_type]] = new_report
                    final_grade = new_grade
                    final_detail = new_detail

                if new_grade in ("A", "B"):
                    repaired = True
                    break

            # Update hard_results with final state
            hard_results[analyst_type] = (final_grade, final_detail)

            # Mark reports that still fail after all attempts
            if not repaired:
                marker = f"\n\n⚠️ **[质量门控: 经 {max_repair_attempts} 次修复仍未通过，评级 {final_grade}]**"
                reports[REPORT_FIELDS[analyst_type]] = current_report + marker

        # ── Rebuild hard summary after repairs ────────────────────────────
        hard_summary_lines = []
        for analyst_type, (grade, detail) in hard_results.items():
            name = ANALYST_NAMES[analyst_type]
            marker = " ⚠️" if grade in ("C", "D", "F") else ""
            hard_summary_lines.append(f"- {name}: [{grade}] {detail}{marker}")
        hard_summary = "\n".join(hard_summary_lines)

        # ── Layer 3: LLM review ───────────────────────────────────────────
        fail_count = sum(
            1 for _, (g, _) in hard_results.items() if g in ("F", "D")
        )

        llm_review = ""
        if fail_count < 4:
            try:
                review_prompt = _build_review_prompt(reports, trade_date, ticker)
                response = llm.invoke(review_prompt)
                llm_review = response.content
            except Exception as e:
                llm_review = f"（LLM 复审失败: {type(e).__name__}: {e}）"

        # ── Build repair summary ──────────────────────────────────────────
        repair_summary = ""
        if repair_log:
            lines = []
            for entry in repair_log:
                status_icon = {
                    "repaired": "✅",
                    "still_failing": "🔄",
                    "error": "❌",
                    "empty": "❌",
                }.get(entry["status"], "❓")
                lines.append(
                    f"- {entry['name']} 第{entry['attempt']}次: "
                    f"{entry['grade_before']}→{entry['grade_after']} {status_icon}"
                    + (f" ({entry.get('error', '')})" if entry.get("error") else "")
                )
            repair_summary = "### 自动修复记录\n" + "\n".join(lines) + "\n\n"

        summary = (
            f"## 数据质量门控结果\n\n"
            f"**标的**: {ticker} | **交易日**: {trade_date}\n\n"
            f"### 硬检查结果\n{hard_summary}\n\n"
            f"{repair_summary}"
            f"### LLM 复审\n"
            f"{llm_review if llm_review else '（跳过 — 多数报告未通过硬检查）'}\n"
        )

        # Build state updates: reports + quality metadata
        updates = {"data_quality_summary": summary}
        for field in REPORT_FIELDS.values():
            if reports[field] != state.get(field, ""):
                updates[field] = reports[field]

        # Repair log as structured data for UI
        repair_log_summary = ""
        if repair_log:
            for entry in repair_log:
                icon = {"repaired": "✅", "still_failing": "🔄", "error": "❌", "empty": "❌"}.get(entry["status"], "❓")
                repair_log_summary += f"{entry['name']} 第{entry['attempt']}次: {entry['grade_before']}→{entry['grade_after']} {icon}\n"
        updates["quality_repair_log"] = repair_log_summary

        return updates

    return quality_gate_node
