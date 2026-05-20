"""Generate PDF reports from analysis results using fpdf2.

Features:
  - Clickable Table of Contents (ToC) page
  - PDF bookmarks (document outline) for each section
  - Markdown hyperlinks → clickable PDF links
  - Chinese CJK font auto-detection
  - Only report content is exported (no token stats)
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.outline import TableOfContents


# ── CJK font detection ──────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Debian/Ubuntu: fonts-noto-cjk
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
    "/usr/share/fonts/noto-cjk/NotoSansCJKsc-Regular.otf",
    # Alpine: font-noto-cjk
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    # CentOS/RHEL
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
]

_CJK_FONT_PATTERNS = ("*CJK*", "*NotoSansSC*", "*PingFang*", "*STHeiti*")
_CJK_FONT_DIRS = ("/usr/share/fonts", "/System/Library/Fonts", "/Library/Fonts")


def _find_cjk_font() -> str | None:
    """Find a CJK-capable font file on the system.

    Search order:
      1. Hardcoded candidate paths (most common locations)
      2. Recursive glob under common font directories
      3. Download NotoSansSC from Google Fonts as last resort (cached)
    """
    # 1. Check known paths
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return path

    # 2. Glob under font directories
    for font_dir in _CJK_FONT_DIRS:
        base = Path(font_dir)
        if not base.is_dir():
            continue
        for pattern in _CJK_FONT_PATTERNS:
            for match in base.rglob(f"{pattern}.ttf"):
                return str(match)
            for match in base.rglob(f"{pattern}.ttc"):
                return str(match)
            for match in base.rglob(f"{pattern}.otf"):
                return str(match)

    # 3. Download NotoSansSC as last resort (cache in /tmp)
    return _download_cjk_font()


def _download_cjk_font() -> str | None:
    """Download NotoSansSC from Google Fonts CDN and cache it.

    Cache priority:
      1. /home/appuser/.tradingagents/fonts/  (persistent volume in Docker)
      2. /tmp/tradingagents_fonts/            (fallback)
    """
    import tempfile
    import urllib.request

    _GSTATIC_URL = (
        "https://fonts.gstatic.com/s/notosanssc/v40/"
        "k3kCo84MPvpLmixcA63oeAL7Iqp5IZJF9bmaG9_FnYw.ttf"
    )

    # Prefer volume-mounted directory for persistence across container restarts
    cache_dirs = [
        Path("/home/appuser/.tradingagents/fonts"),
        Path(tempfile.gettempdir()) / "tradingagents_fonts",
    ]

    for cache_dir in cache_dirs:
        cache_path = cache_dir / "NotoSansSC-Regular.ttf"
        if cache_path.is_file() and cache_path.stat().st_size > 1_000_000:
            return str(cache_path)

    # Download to first writable directory
    for cache_dir in cache_dirs:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / "NotoSansSC-Regular.ttf"
            urllib.request.urlretrieve(_GSTATIC_URL, str(cache_path))
            if cache_path.stat().st_size > 1_000_000:
                return str(cache_path)
            cache_path.unlink(missing_ok=True)
        except Exception:
            continue

    return None


# ── Text helpers ────────────────────────────────────────────────────────────

def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def _strip_md_inline(text: str) -> str:
    """Remove inline markdown formatting: **bold**, *italic*, `code`."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    return text


# ── Report sections ────────────────────────────────────────────────────────

_REPORT_SECTIONS = [
    ("market_report", "技术分析报告"),
    ("sentiment_report", "市场情绪报告"),
    ("news_report", "新闻舆情报告"),
    ("fundamentals_report", "基本面报告"),
    ("policy_report", "政策分析报告"),
    ("hot_money_report", "游资追踪报告"),
    ("lockup_report", "解禁/减持报告"),
]

_DEBATE_SUBSECTIONS = [
    ("bull_history", "多方论点"),
    ("bear_history", "空方论点"),
    ("judge_decision", "研究经理决策"),
]

_RISK_SUBSECTIONS = [
    ("aggressive_history", "激进观点"),
    ("conservative_history", "保守观点"),
    ("neutral_history", "中性观点"),
    ("judge_decision", "风控决策"),
]


# ── Custom TableOfContents renderer ────────────────────────────────────────

class _CjkTableOfContents(TableOfContents):
    """TableOfContents that uses CJK font when available."""

    def __init__(self, has_cjk: bool = False) -> None:
        super().__init__()
        self.has_cjk = has_cjk

    def render_toc(self, pdf: FPDF, outline: list) -> None:
        pdf._use_font("B", 18)
        pdf.set_text_color(255, 90, 31)
        pdf.cell(0, 12, "目  录", align="C", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(8)

        for section in outline:
            level = section.level
            indent = level * 8
            pdf.set_x(pdf.l_margin + indent)
            pdf._use_font("B" if level == 0 else "", 11 if level == 0 else 10)
            pdf.set_text_color(40, 40, 40)

            name = section.name
            page = section.page_number

            # Draw dotted line between name and page number
            name_w = pdf.get_string_width(name) + 2
            page_str = str(page)
            page_w = pdf.get_string_width(page_str) + 2
            avail = pdf.w - pdf.r_margin - pdf.get_x() - page_w

            pdf.cell(name_w, 7, name)

            if name_w < avail:
                # Draw dots
                dot_w = pdf.get_string_width(".")
                x_start = pdf.get_x()
                num_dots = int((avail - name_w) / dot_w)
                dot_str = " " + "." * num_dots
                pdf._use_font("", 8)
                pdf.set_text_color(160, 160, 160)
                pdf.cell(avail - name_w, 7, dot_str)
                pdf._use_font("B" if level == 0 else "", 11 if level == 0 else 10)
                pdf.set_text_color(40, 40, 40)

            # Link the page number to the actual page
            link = pdf.add_link(page=page)
            pdf.cell(page_w, 7, page_str, align="R", link=link)
            pdf.ln(7)


# ── PDF class ───────────────────────────────────────────────────────────────

class _ReportPDF(FPDF):
    def __init__(self, ticker: str, trade_date: str, signal: str) -> None:
        super().__init__()
        self.ticker = ticker
        self.trade_date = trade_date
        self.signal = signal
        self._has_cjk = False

        font_path = _find_cjk_font()
        if font_path:
            is_ttc = font_path.lower().endswith(".ttc")
            try:
                if is_ttc:
                    # TTC files contain multiple fonts; try font number 0 (Regular)
                    self.add_font("CJK", "", font_path, collection_font_number=0)
                    self.add_font("CJK", "B", font_path, collection_font_number=1)
                else:
                    self.add_font("CJK", "", font_path)
                    self.add_font("CJK", "B", font_path)
                self._has_cjk = True
            except Exception:
                # If font loading fails, fall back gracefully
                self._has_cjk = False

    def _use_font(self, style: str = "", size: int = 10) -> None:
        if self._has_cjk:
            self.set_font("CJK", style, size)
        else:
            self.set_font("Helvetica", style, size)

    def header(self) -> None:
        self._use_font("", 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 6, f"A股多Agent投研分析  |  {self.ticker}  |  {self.trade_date}", align="C")
        self.ln(8)
        self.set_draw_color(60, 60, 60)
        self.line(10, self.get_y(), self.w - 10, self.get_y())
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-15)
        self._use_font("", 8)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

    # ── Cover page ─────────────────────────────────────────────────────────

    def add_cover(self) -> None:
        self.add_page()
        self.ln(60)

        self._use_font("B", 24)
        self.set_text_color(255, 90, 31)
        self.cell(0, 12, "A股多Agent投研分析报告", align="C")
        self.ln(20)

        self._use_font("B", 36)
        self.set_text_color(30, 30, 30)
        self.cell(0, 18, self.ticker, align="C")
        self.ln(16)

        self._use_font("", 14)
        self.set_text_color(100, 100, 100)
        self.cell(0, 10, f"分析日期: {self.trade_date}", align="C")
        self.ln(8)
        self.cell(0, 10, f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}", align="C")
        self.ln(20)

        # Signal
        s = self.signal.upper()
        if "BUY" in s:
            r, g, b = 34, 197, 94
        elif "SELL" in s:
            r, g, b = 239, 68, 68
        else:
            r, g, b = 251, 191, 36
        self._use_font("B", 40)
        self.set_text_color(r, g, b)
        self.cell(0, 20, s, align="C")
        self.ln(20)

        self._use_font("", 9)
        self.set_text_color(120, 120, 120)
        self.multi_cell(
            0, 5,
            "免责声明: 本报告由 AI 多 Agent 系统自动生成, 仅供学习研究与技术演示, "
            "不构成任何投资建议。投资决策请咨询持牌专业机构。"
            "使用本报告所产生的任何损失由使用者自行承担。",
            align="C",
        )

    # ── Section with bookmark ───────────────────────────────────────────────

    def add_section(self, title: str, content: str, level: int = 0) -> None:
        """Add a section with a PDF bookmark at the given outline level."""
        self.add_page()
        self.start_section(name=title, level=level)

        self._use_font("B", 16)
        self.set_text_color(255, 90, 31)
        self.cell(0, 10, title)
        self.ln(12)

        cleaned = _strip_think(content)
        self._render_markdown(cleaned, parent_level=level)

    def add_subsection(self, title: str, content: str, level: int = 1) -> None:
        """Add a subsection with a nested PDF bookmark."""
        # No page break for subsections
        self.start_section(name=title, level=level)
        self.ln(4)
        self._use_font("B", 13)
        self.set_text_color(255, 90, 31)
        self.cell(0, 8, title)
        self.ln(9)

        cleaned = _strip_think(content)
        self._render_markdown(cleaned, parent_level=level)

    # ── Markdown renderer with hyperlink support ───────────────────────────

    def _render_table_block(self, header: list[str], rows: list[list[str]],
                            aligns: list[str] | None = None) -> None:
        """Render a markdown table block using fpdf2 Table API.

        Features: bordered cells, header with colored background, zebra striping,
        and auto text wrapping (no 'not enough horizontal space' error).

        Args:
            header: List of header cell texts.
            rows: List of rows, each a list of cell texts.
            aligns: Optional list of align strings ('L','C','R') per column,
                    parsed from markdown separator line (e.g. :---: means center).
        """
        from fpdf.fonts import FontFace
        from fpdf.enums import Align

        num_cols = len(header) or (len(rows[0]) if rows else 0)
        if num_cols == 0:
            return

        # Build align mapping
        col_aligns: list[str] = []
        for i in range(num_cols):
            if aligns and i < len(aligns):
                col_aligns.append(aligns[i])
            else:
                col_aligns.append("L")

        # Headings style (same font family, bold + dark background + white text)
        kwargs: dict[str, Any] = {
            "borders_layout": "MINIMAL",
            "cell_fill_color": (245, 245, 255),
            "cell_fill_mode": "ROWS",
            "first_row_as_headings": True,
            "headings_style": FontFace(
                emphasis="BOLD",
                fill_color=(60, 60, 120),
                color=(255, 255, 255),
            ),
        }

        # Choose a smaller font for tables with many columns
        font_size = 8 if num_cols > 4 else 9
        self._use_font("", font_size)

        def _clean(text: str) -> str:
            """Strip markdown inline formatting for display."""
            return _strip_md_inline(text).strip()

        with self.table(**kwargs) as table:
            # Header row
            hrow = table.row()
            for i, h in enumerate(header):
                align = Align[col_aligns[i]] if i < len(col_aligns) else Align.L
                hrow.cell(_clean(h), align=align)
            # Data rows
            for row_data in rows:
                drow = table.row()
                for i, cell in enumerate(row_data):
                    align = Align[col_aligns[i]] if i < len(col_aligns) else Align.L
                    drow.cell(_clean(cell), align=align)

        # Reset font after table
        self._use_font("", 10)

    def _render_markdown(self, text: str, parent_level: int = 0) -> None:
        """Render markdown text with basic styling and clickable hyperlinks.

        Headings inside the markdown content are mapped to outline levels
        relative to *parent_level* so that the PDF bookmark hierarchy stays
        coherent (fpdf2 forbids skipping levels, e.g. 0 → 2).
            #  → parent_level + 1  (or parent_level + 2 if parent is 0 and no ## precedes)
            ## → parent_level + 1
            ### → parent_level + 2

        Because LLM output may jump directly from the section title to a
        sub-sub-heading (e.g. ``###`` without a preceding ``##``), we
        demote headings that would cause a skip: the child level is
        ``min(parent_level + heading_depth, last_level + 1)``.
        """
        lines = text.split("\n")
        i = 0
        last_section_level = parent_level  # track the deepest bookmark level used

        # --- Table accumulation state ---
        table_header: list[str] = []
        table_rows: list[list[str]] = []
        table_aligns: list[str] = []
        in_table = False

        def _flush_table() -> None:
            """Flush accumulated table rows to PDF."""
            nonlocal table_header, table_rows, table_aligns, in_table
            if table_header or table_rows:
                self._render_table_block(table_header, table_rows, table_aligns or None)
                self.ln(3)
            table_header = []
            table_rows = []
            table_aligns = []
            in_table = False

        def _parse_table_align(sep_line: str) -> list[str]:
            """Parse markdown table separator line for column alignments.

            Examples: ':---:' = center, ':---' = left, '---:' = right, '---' = left
            """
            parts = sep_line.strip("|").split("|")
            aligns: list[str] = []
            for p in parts:
                p = p.strip()
                if p.startswith(":") and p.endswith(":"):
                    aligns.append("C")
                elif p.endswith(":"):
                    aligns.append("R")
                else:
                    aligns.append("L")
            return aligns

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # --- Table row detection ---
            if stripped.startswith("|") and stripped.endswith("|"):
                # Check if it's a separator line (|---|---|)
                if re.match(r"^\|[-:\s|]+\|$", stripped):
                    table_aligns = _parse_table_align(stripped)
                    in_table = True
                    i += 1
                    continue
                # Parse cells
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                if not in_table and not table_header:
                    # First row is the header
                    table_header = cells
                    in_table = True
                else:
                    table_rows.append(cells)
                i += 1
                continue

            # If we were in a table and hit a non-table line, flush it
            if in_table:
                _flush_table()

            # Empty line → small gap
            if not stripped:
                self.ln(3)
                i += 1
                continue

            # Headings — compute safe outline level
            if stripped.startswith("#"):
                hashes = len(stripped) - len(stripped.lstrip("#"))
                heading_text = stripped.lstrip("#").strip()
                # Desired outline level relative to parent
                desired_level = parent_level + hashes
                # Ensure no level-skipping: can only go one deeper than last
                safe_level = min(desired_level, last_section_level + 1)
                last_section_level = safe_level

                self.start_section(name=heading_text, level=safe_level)

                # Visual style depends on heading depth (not outline level)
                if hashes >= 3:
                    self._use_font("B", 11)
                    self.set_text_color(50, 50, 50)
                    self.cell(0, 7, heading_text)
                    self.ln(8)
                elif hashes == 2:
                    self._use_font("B", 13)
                    self.set_text_color(40, 40, 40)
                    self.cell(0, 8, heading_text)
                    self.ln(9)
                else:
                    self._use_font("B", 14)
                    self.set_text_color(255, 90, 31)
                    self.cell(0, 9, heading_text)
                    self.ln(10)
                i += 1
                continue

            # Horizontal rule
            if stripped in ("---", "***", "___"):
                self.set_draw_color(180, 180, 180)
                y = self.get_y() + 2
                self.line(10, y, self.w - 10, y)
                self.ln(6)
                i += 1
                continue

            # Bullet / numbered list
            if re.match(r"^[-*]\s", stripped) or re.match(r"^\d+[.)]\s", stripped):
                self._use_font("", 10)
                self.set_text_color(40, 40, 40)
                if re.match(r"^[-*]\s", stripped):
                    bullet = "  •  "
                    body = stripped[2:].strip()
                else:
                    m = re.match(r"^(\d+[.)])\s*(.*)", stripped)
                    bullet = f"  {m.group(1)} "
                    body = m.group(2)
                self._render_line_with_links(bullet, body)
                i += 1
                continue

            # Regular paragraph — collect consecutive non-special lines
            para_lines = []
            while i < len(lines):
                ln = lines[i].strip()
                if (not ln or ln.startswith("#") or ln.startswith("|")
                        or re.match(r"^[-*]\s", ln)
                        or re.match(r"^\d+[.)]\s", ln)
                        or ln in ("---", "***", "___")):
                    break
                para_lines.append(ln)
                i += 1

            if para_lines:
                self._use_font("", 10)
                self.set_text_color(40, 40, 40)
                para = " ".join(para_lines)
                self._render_paragraph_with_links(para)
                self.ln(2)
                continue

            i += 1

        # Flush any remaining table at end of text
        if in_table:
            _flush_table()

    def _render_text_segments(self, segments: list[tuple[str, str | None]]) -> None:
        """Render a sequence of (text, url_or_none) segments with auto-wrap.

        Segments are rendered using cell() for inline placement.  When the
        remaining horizontal space is insufficient, a line break is inserted
        before continuing.  This prevents the "not enough horizontal space"
        FPDFException.
        """
        for text, url in segments:
            if not text:
                continue
            remaining_w = self.w - self.r_margin - self.get_x()
            text_w = self.get_string_width(text)
            # If current segment doesn't fit, wrap to next line first
            if text_w > remaining_w and self.get_x() > self.l_margin:
                self.ln(5.5)
                self.set_x(self.l_margin)
                remaining_w = self.w - self.r_margin - self.l_margin
            # Segment is wider than a full line → use multi_cell for auto-wrap
            if text_w > remaining_w:
                if url:
                    self.set_text_color(30, 100, 200)
                    self.multi_cell(0, 5.5, text, link=url)
                    self.set_text_color(40, 40, 40)
                else:
                    self.multi_cell(0, 5.5, text)
                # multi_cell resets x to l_margin; re-position for next segment
                self.set_x(self.l_margin)
            else:
                if url:
                    self.set_text_color(30, 100, 200)
                    self.cell(text_w, 5.5, text, link=url)
                    self.set_text_color(40, 40, 40)
                else:
                    self.cell(text_w, 5.5, text)
        self.ln(5.5)
        # Ensure x is at left margin for next element
        self.set_x(self.l_margin)

    @staticmethod
    def _split_rich(text: str) -> list[tuple[str, str | None]]:
        """Split text with markdown links into [(text, None), (label, url), ...]."""
        segments: list[tuple[str, str | None]] = []
        pattern = r"\[(.+?)\]\((.+?)\)"
        pos = 0
        for m in re.finditer(pattern, text):
            plain = text[pos:m.start()]
            if plain:
                segments.append((_strip_md_inline(plain), None))
            label = _strip_md_inline(m.group(1))
            url = m.group(2)
            segments.append((label, url))
            pos = m.end()
        tail = text[pos:]
        if tail:
            segments.append((_strip_md_inline(tail), None))
        return segments

    def _render_line_with_links(self, prefix: str, body: str) -> None:
        """Render a line that may contain markdown links as clickable PDF links."""
        # Combine prefix + body so multi_cell handles wrapping in one call
        combined = prefix + body if prefix else body
        links = re.findall(r"\[(.+?)\]\((.+?)\)", combined)
        if not links:
            # No links → plain text, use multi_cell for auto-wrap
            text = _strip_md_inline(combined)
            self.multi_cell(0, 5.5, text)
            # multi_cell leaves x at right margin — reset to left margin
            self.set_x(self.l_margin)
            return

        # Has links → render as inline segments
        segments = self._split_rich(combined)
        self._render_text_segments(segments)

    def _render_paragraph_with_links(self, text: str) -> None:
        """Render a paragraph that may contain markdown links as clickable PDF links."""
        links = re.findall(r"\[(.+?)\]\((.+?)\)", text)
        if not links:
            text = _strip_md_inline(text)
            self.multi_cell(0, 5.5, text)
            # multi_cell leaves x at right margin — reset to left margin
            self.set_x(self.l_margin)
            return

        # Has links → render as inline segments
        segments = self._split_rich(text)
        self._render_text_segments(segments)


# ── Public API ──────────────────────────────────────────────────────────────

def generate_pdf(final_state: dict[str, Any], ticker: str, trade_date: str, signal: str) -> bytes:
    """Generate a PDF report with ToC, bookmarks, and clickable hyperlinks."""
    pdf = _ReportPDF(ticker, trade_date, signal)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=20)

    # ── Cover page ─────────────────────────────────────────────────────────
    pdf.add_cover()

    # ── Insert ToC placeholder (2 pages reserved) ─────────────────────────
    toc = _CjkTableOfContents(has_cjk=pdf._has_cjk)
    pdf.insert_toc_placeholder(toc.render_toc, pages=2, allow_extra_pages=True)

    # ── Analyst reports ────────────────────────────────────────────────────
    for key, title in _REPORT_SECTIONS:
        content = final_state.get(key, "")
        if content:
            pdf.add_section(title, str(content), level=0)

    # ── Investment debate ──────────────────────────────────────────────────
    debate = final_state.get("investment_debate_state")
    if debate and isinstance(debate, dict):
        has_any = any(debate.get(k) for k, _ in _DEBATE_SUBSECTIONS)
        if has_any:
            pdf.add_page()
            pdf.start_section(name="多空辩论", level=0)
            pdf._use_font("B", 16)
            pdf.set_text_color(255, 90, 31)
            pdf.cell(0, 10, "多空辩论")
            pdf.ln(12)

            for key, label in _DEBATE_SUBSECTIONS:
                sub = debate.get(key, "")
                if sub:
                    pdf.add_subsection(label, str(sub), level=1)

    # ── Trader decision ────────────────────────────────────────────────────
    trader_decision = final_state.get("trader_investment_decision", "")
    if trader_decision:
        pdf.add_section("交易员决策", _strip_think(str(trader_decision)), level=0)

    # ── Final investment plan ──────────────────────────────────────────────
    inv_plan = final_state.get("investment_plan", "")
    if inv_plan:
        pdf.add_section("最终投资建议", _strip_think(str(inv_plan)), level=0)

    # ── Risk debate ────────────────────────────────────────────────────────
    risk = final_state.get("risk_debate_state")
    if risk and isinstance(risk, dict):
        has_any = any(risk.get(k) for k, _ in _RISK_SUBSECTIONS)
        if has_any:
            pdf.add_page()
            pdf.start_section(name="风控评估", level=0)
            pdf._use_font("B", 16)
            pdf.set_text_color(255, 90, 31)
            pdf.cell(0, 10, "风控评估")
            pdf.ln(12)

            for key, label in _RISK_SUBSECTIONS:
                sub = risk.get(key, "")
                if sub:
                    pdf.add_subsection(label, str(sub), level=1)

    # ── Final decision ─────────────────────────────────────────────────────
    final_decision = final_state.get("final_trade_decision", "")
    if final_decision:
        pdf.add_section("最终决策", _strip_think(str(final_decision)), level=0)

    return bytes(pdf.output())
