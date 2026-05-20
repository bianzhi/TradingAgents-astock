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
        self._render_markdown(cleaned)

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
        self._render_markdown(cleaned)

    # ── Markdown renderer with hyperlink support ───────────────────────────

    def _render_markdown(self, text: str) -> None:
        """Render markdown text with basic styling and clickable hyperlinks."""
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Empty line → small gap
            if not stripped:
                self.ln(3)
                i += 1
                continue

            # Headings
            if stripped.startswith("###"):
                self.start_section(name=stripped.lstrip("#").strip(), level=2)
                self._use_font("B", 11)
                self.set_text_color(50, 50, 50)
                self.cell(0, 7, stripped.lstrip("#").strip())
                self.ln(8)
                i += 1
                continue
            if stripped.startswith("##"):
                self.start_section(name=stripped.lstrip("#").strip(), level=1)
                self._use_font("B", 13)
                self.set_text_color(40, 40, 40)
                self.cell(0, 8, stripped.lstrip("#").strip())
                self.ln(9)
                i += 1
                continue
            if stripped.startswith("#"):
                self.start_section(name=stripped.lstrip("#").strip(), level=1)
                self._use_font("B", 14)
                self.set_text_color(255, 90, 31)
                self.cell(0, 9, stripped.lstrip("#").strip())
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

            # Table rows
            if stripped.startswith("|") and stripped.endswith("|"):
                if re.match(r"^\|[-:\s|]+\|$", stripped):
                    i += 1
                    continue
                self._use_font("", 9)
                self.set_text_color(60, 60, 60)
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                self._render_table_row_with_links(cells)
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

    def _render_text_segments(self, segments: list[tuple[str, str | None]]) -> None:
        """Render a sequence of (text, url_or_none) segments on one line.

        Segments are rendered left-to-right using cell() so they stay on
        the same line.  If a url is provided the text is clickable and
        rendered in blue.
        """
        for text, url in segments:
            if not text:
                continue
            if url:
                self.set_text_color(30, 100, 200)
                self.cell(self.get_string_width(text), 5.5, text, link=url)
                self.set_text_color(40, 40, 40)
            else:
                self.cell(self.get_string_width(text), 5.5, text)
        self.ln(5.5)

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
        links = re.findall(r"\[(.+?)\]\((.+?)\)", body)
        if not links:
            # No links → plain text, use multi_cell for auto-wrap
            text = _strip_md_inline(body)
            self.cell(self.get_string_width(prefix), 5.5, prefix)
            remaining_w = self.w - self.r_margin - self.get_x()
            if remaining_w < self.get_string_width(text):
                # Text too long for remaining line, use multi_cell on the remainder
                self.multi_cell(0, 5.5, text)
            else:
                self.cell(0, 5.5, text)
                self.ln(5.5)
            return

        # Split into segments and render inline
        segments: list[tuple[str, str | None]] = []
        if prefix:
            segments.append((prefix, None))
        segments.extend(self._split_rich(body))
        self._render_text_segments(segments)

    def _render_paragraph_with_links(self, text: str) -> None:
        """Render a paragraph that may contain markdown links as clickable PDF links."""
        links = re.findall(r"\[(.+?)\]\((.+?)\)", text)
        if not links:
            text = _strip_md_inline(text)
            self.multi_cell(0, 5.5, text)
            return

        # Has links → render as inline segments
        segments = self._split_rich(text)
        self._render_text_segments(segments)

    def _render_table_row_with_links(self, cells: list[str]) -> None:
        """Render a table row, each cell may contain links."""
        col_w = (self.w - self.l_margin - self.r_margin) / max(len(cells), 1)
        for cell_text in cells:
            links = re.findall(r"\[(.+?)\]\((.+?)\)", cell_text)
            clean = _strip_md_inline(cell_text)
            # Remove markdown link syntax for display text, but keep URL clickable
            display = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", clean)
            if links:
                # Use the first link as the cell link
                self.cell(col_w, 5, display, link=links[0][1])
            else:
                self.cell(col_w, 5, clean)
        self.ln(5)


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
