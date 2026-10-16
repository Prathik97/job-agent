"""Render the resume as DOCX and PDF. Single column, standard headings, real text, no tables
or images. The PDF is measured, and the page limit is enforced on the generated PDF."""
from __future__ import annotations

import io
from pathlib import Path
from typing import List, Optional, Tuple

from tailor import layout

Block = Tuple[str, ...]   # ("para", text) | ("role", title_line, meta_line) | ("bullet", text)


class RenderError(Exception):
    pass


def pdf_page_count(data: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(data)).pages)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)


def _register_font() -> Tuple[str, str]:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for regular, bold in layout.FONT_CANDIDATES:
        if Path(regular).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont(layout.FONT_NAME, regular))
            pdfmetrics.registerFont(TTFont(layout.FONT_NAME + "-Bold", bold))
            return layout.FONT_NAME, layout.FONT_NAME + "-Bold"
    raise RenderError("no font with the rupee sign found; add one to tailor/layout.py FONT_CANDIDATES")


def build_pdf(name: str, contact: str, sections: List[Tuple[str, List[Block]]], size: float) -> bytes:
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    regular, bold = _register_font()
    base = ParagraphStyle("base", fontName=regular, fontSize=size, leading=size * 1.28, alignment=TA_LEFT)
    title = ParagraphStyle("name", parent=base, fontName=bold, fontSize=size + 6, leading=(size + 6) * 1.2)
    head = ParagraphStyle("head", parent=base, fontName=bold, fontSize=size + 1.5, spaceBefore=size * 0.7,
                          spaceAfter=size * 0.2, keepWithNext=1)
    role = ParagraphStyle("role", parent=base, fontName=bold, spaceBefore=size * 0.4, keepWithNext=1)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=layout.MARGIN_INCH * inch, rightMargin=layout.MARGIN_INCH * inch,
                            topMargin=layout.MARGIN_INCH * inch, bottomMargin=layout.MARGIN_INCH * inch,
                            title=f"{name} resume", author=name)
    story = [Paragraph(escape(name), title), Paragraph(escape(contact), base)]
    for heading, blocks in sections:
        start = len(story)
        story.append(Paragraph(escape(heading), head))
        bullets: List = []

        def flush():
            if bullets:
                story.append(ListFlowable(list(bullets), bulletType="bullet", start="•", leftIndent=14,
                                          bulletFontName=regular, bulletFontSize=size))
                bullets.clear()
        for b in blocks:
            if b[0] == "bullet":
                bullets.append(ListItem(Paragraph(escape(b[1]), base), leftIndent=14))
                continue
            flush()
            if b[0] == "role":
                story += [Paragraph(escape(b[1]), role), Paragraph(escape(b[2]), base)]
            else:
                story.append(Paragraph(escape(b[1]), base))
        flush()
        if len(blocks) <= 4 and all(b[0] != "role" for b in blocks):   # short sections stay on one page
            story[start:] = [KeepTogether(story[start:])]
    story.append(Spacer(1, 1))
    doc.build(story)
    return buf.getvalue()


def build_docx(name: str, contact: str, sections: List[Tuple[str, List[Block]]], size: float) -> bytes:
    from docx import Document
    from docx.shared import Inches, Pt

    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = s.top_margin = s.bottom_margin = Inches(layout.MARGIN_INCH)
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = layout.FONT_NAME, Pt(size)
    normal.paragraph_format.space_after = Pt(1)
    for style in ("Heading 1", "List Bullet", "Title"):
        doc.styles[style].font.name = layout.FONT_NAME
    doc.styles["Heading 1"].font.size = Pt(size + 1.5)
    doc.styles["Title"].font.size = Pt(size + 6)
    doc.add_paragraph(name, style="Title")
    doc.add_paragraph(contact)
    for heading, blocks in sections:
        doc.add_paragraph(heading, style="Heading 1")
        for b in blocks:
            if b[0] == "bullet":
                doc.add_paragraph(b[1], style="List Bullet")
            elif b[0] == "role":
                doc.add_paragraph().add_run(b[1]).bold = True
                doc.add_paragraph(b[2])
            else:
                doc.add_paragraph(b[1])
    doc.core_properties.author = name
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def fit_pdf(name: str, contact: str, sections: List[Tuple[str, List[Block]]]) -> Tuple[bytes, float, int]:
    """Smallest-change layout that fits: try each font size; returns (pdf, size, pages)."""
    pages = 0
    for size in layout.FONT_SIZES:
        pdf = build_pdf(name, contact, sections, size)
        pages = pdf_page_count(pdf)
        if pages <= layout.MAX_PAGES:
            return pdf, size, pages
    return pdf, layout.FONT_SIZES[-1], pages
