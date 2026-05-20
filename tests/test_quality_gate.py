"""Tests for quality gate auto-repair logic."""

import pytest

from tradingagents.agents.quality_gate import (
    _hard_check_report,
    _extract_missing_items,
    _grade_improved,
    MAX_REPAIR_ATTEMPTS,
    REPORT_FIELDS,
    ANALYST_NAMES,
    ANALYST_FACTORIES,
)


# ── Grade comparison ──────────────────────────────────────────────────────────

class TestGradeImproved:
    def test_a_beats_b(self):
        assert _grade_improved("A", "B") is True

    def test_a_beats_c(self):
        assert _grade_improved("A", "C") is True

    def test_b_beats_c(self):
        assert _grade_improved("B", "C") is True

    def test_c_not_improved_from_c(self):
        assert _grade_improved("C", "C") is False

    def test_d_worse_than_c(self):
        assert _grade_improved("D", "C") is False

    def test_f_worse_than_d(self):
        assert _grade_improved("F", "D") is False

    def test_same_grade_no_improvement(self):
        for g in ("A", "B", "C", "D", "F"):
            assert _grade_improved(g, g) is False


# ── Hard check ────────────────────────────────────────────────────────────────

class TestHardCheck:
    def test_empty_report_f(self):
        grade, detail = _hard_check_report("market", "")
        assert grade == "F"

    def test_short_report_d(self):
        grade, detail = _hard_check_report("market", "x" * 50)
        assert grade == "D"

    def test_missing_data_c(self):
        report = "x" * 300 + "\n[数据缺失: item1]\n[数据缺失: item2]\n[数据缺失: item3]"
        grade, detail = _hard_check_report("market", report)
        assert grade == "C"
        assert "3 处数据缺失" in detail

    def test_missing_data_b(self):
        report = "x" * 300 + "\n|---|\n[数据缺失: item1]"
        grade, detail = _hard_check_report("market", report)
        assert grade == "B"

    def test_good_report_a(self):
        report = (
            "Report content with enough length. " * 20
            + "\n| Col | Val |\n|---|---|\n| a | b |"
        )
        grade, detail = _hard_check_report("market", report)
        assert grade == "A"

    def test_no_table_b(self):
        report = "x" * 400
        grade, detail = _hard_check_report("market", report)
        assert grade == "B"
        assert "缺少汇总表格" in detail

    def test_failure_markers_d(self):
        report = "无法获取数据 " * 50
        grade, detail = _hard_check_report("market", report)
        assert grade == "D"


# ── Missing items extraction ──────────────────────────────────────────────────

class TestExtractMissingItems:
    def test_no_missing(self):
        assert _extract_missing_items("报告内容") == []

    def test_one_missing(self):
        result = _extract_missing_items("报告 [数据缺失: PE数据] 内容")
        assert result == ["PE数据"]

    def test_multiple_missing(self):
        text = "a [数据缺失: x] b [数据缺失: y] c"
        result = _extract_missing_items(text)
        assert result == ["x", "y"]

    def test_chinese_colon(self):
        result = _extract_missing_items("报告 [数据缺失：资产负债表] 内容")
        assert result == ["资产负债表"]


# ── Constants validation ──────────────────────────────────────────────────────

class TestConstants:
    def test_all_analysts_have_names(self):
        for at in REPORT_FIELDS:
            assert at in ANALYST_NAMES, f"Missing name for {at}"

    def test_all_analysts_have_factories(self):
        for at in REPORT_FIELDS:
            assert at in ANALYST_FACTORIES, f"Missing factory for {at}"

    def test_max_repair_attempts(self):
        assert MAX_REPAIR_ATTEMPTS == 3
