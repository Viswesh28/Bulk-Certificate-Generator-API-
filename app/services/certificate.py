"""The single predefined certificate template, rendered to PDF with ReportLab.

Layout (landscape A4, 842 x 595 pt, origin at bottom-left):

    ┌──────────────────────────────────────────────────────────┐
    │  double border (navy + gold) with gold corner diamonds   │
    │                                                          │
    │                 ISSUER NAME  (letterhead)                │
    │             Certificate of Completion                    │
    │                    ──◆──                                 │
    │              This is to certify that                     │
    │               «Recipient Name»   (auto-fit)              │
    │           has successfully completed the course          │
    │               «Course Title»     (auto-fit)              │
    │                                                          │
    │   ______________          (◉ seal)        ______________ │
    │   Signatory name        Certificate ID    Date           │
    └──────────────────────────────────────────────────────────┘

Design notes:
* Only PDF base-14 fonts (Times / Helvetica / Courier) are used, so no font
  files ship with the project. The trade-off is WinAnsi-only text; the
  validation layer rejects characters these fonts cannot render.
* Name and course title are *auto-fitted*: the font size shrinks until the
  text fits its box, so a 180-character name cannot overflow the border.
* The renderer is a pure function ``CertificateData -> bytes``: no I/O, no
  database, trivial to unit-test and to run in a worker thread.
"""
from __future__ import annotations

import io
import math
from dataclasses import dataclass
from datetime import date

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas as canvas_mod

PAGE_W, PAGE_H = landscape(A4)

NAVY = HexColor("#1F3864")
GOLD = HexColor("#B08D24")
GOLD_SOFT = HexColor("#E9DFC0")
INK = HexColor("#212121")
MUTED = HexColor("#6E6E6E")


@dataclass(frozen=True)
class CertificateData:
    """Everything the template needs to render one certificate."""

    recipient_name: str
    course_title: str
    issuer_name: str
    issue_date: date
    signatory_name: str | None
    signatory_title: str | None
    certificate_uid: str


def render_certificate_pdf(data: CertificateData) -> bytes:
    """Render one certificate and return the PDF file contents."""
    buf = io.BytesIO()
    c = canvas_mod.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"Certificate - {data.recipient_name}")
    c.setAuthor(data.issuer_name)
    c.setSubject(data.course_title)

    _draw_border(c)
    _draw_header(c, data)
    _draw_body(c, data)
    _draw_footer(c, data)

    c.showPage()
    c.save()
    return buf.getvalue()


def format_issue_date(d: date) -> str:
    """'1 October 2026' (no leading zero — portable across platforms)."""
    return f"{d.day} {d.strftime('%B')} {d.year}"


# --------------------------------------------------------------------------- #
# drawing helpers
# --------------------------------------------------------------------------- #

def _center(c, text: str, y: float, font: str, size: float, color: Color) -> None:
    c.setFont(font, size)
    c.setFillColor(color)
    c.drawCentredString(PAGE_W / 2, y, text)


def _fit_size(text: str, font: str, start: float, minimum: float, max_width: float) -> float:
    """Largest size <= start (down to minimum) at which text fits max_width."""
    size = start
    while size > minimum and pdfmetrics.stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def _diamond(c, x: float, y: float, r: float, color: Color) -> None:
    c.setFillColor(color)
    c.setStrokeColor(color)
    p = c.beginPath()
    p.moveTo(x, y + r)
    p.lineTo(x + r, y)
    p.lineTo(x, y - r)
    p.lineTo(x - r, y)
    p.close()
    c.drawPath(p, fill=1, stroke=0)


def _star(c, x: float, y: float, r_outer: float, color: Color, points: int = 5) -> None:
    """Filled n-pointed star centred at (x, y) — used inside the seal."""
    r_inner = r_outer * 0.45
    p = c.beginPath()
    for i in range(points * 2):
        r = r_outer if i % 2 == 0 else r_inner
        # -90° so the first tip points straight up
        angle = math.pi / points * i - math.pi / 2
        px, py = x + r * math.cos(angle), y + r * math.sin(angle)
        if i == 0:
            p.moveTo(px, py)
        else:
            p.lineTo(px, py)
    p.close()
    c.setFillColor(color)
    c.drawPath(p, fill=1, stroke=0)


# --------------------------------------------------------------------------- #
# template sections
# --------------------------------------------------------------------------- #

def _draw_border(c) -> None:
    c.setStrokeColor(NAVY)
    c.setLineWidth(3)
    c.rect(18, 18, PAGE_W - 36, PAGE_H - 36)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.2)
    c.rect(27, 27, PAGE_W - 54, PAGE_H - 54)
    for x, y in [
        (27, 27),
        (PAGE_W - 27, 27),
        (27, PAGE_H - 27),
        (PAGE_W - 27, PAGE_H - 27),
    ]:
        _diamond(c, x, y, 5.5, GOLD)


def _draw_header(c, data: CertificateData) -> None:
    _center(c, data.issuer_name.upper(), PAGE_H - 78, "Helvetica-Bold", 14, NAVY)
    _center(c, "Certificate of Completion", PAGE_H - 138, "Times-Bold", 38, INK)

    # gold ornament: ────◆────
    y = PAGE_H - 160
    c.setStrokeColor(GOLD)
    c.setLineWidth(1)
    c.line(PAGE_W / 2 - 110, y, PAGE_W / 2 - 12, y)
    c.line(PAGE_W / 2 + 12, y, PAGE_W / 2 + 110, y)
    _diamond(c, PAGE_W / 2, y, 4.5, GOLD)


def _draw_body(c, data: CertificateData) -> None:
    _center(c, "This is to certify that", PAGE_H - 210, "Times-Italic", 13, MUTED)

    # Recipient name: auto-fit so even very long names stay inside the border.
    name = data.recipient_name
    name_size = _fit_size(name, "Times-Bold", 34, 13, PAGE_W - 240)
    _center(c, name, PAGE_H - 258, "Times-Bold", name_size, NAVY)

    # thin gold rule under the name, at most as wide as the name itself
    name_w = min(pdfmetrics.stringWidth(name, "Times-Bold", name_size), PAGE_W - 240)
    y = PAGE_H - 272
    c.setStrokeColor(GOLD_SOFT)
    c.setLineWidth(1)
    c.line(PAGE_W / 2 - name_w / 2, y, PAGE_W / 2 + name_w / 2, y)

    _center(c, "has successfully completed the course", PAGE_H - 305, "Times-Italic", 13, MUTED)

    course_size = _fit_size(data.course_title, "Helvetica-Bold", 19, 10, PAGE_W - 280)
    _center(c, data.course_title, PAGE_H - 335, "Helvetica-Bold", course_size, INK)


def _draw_footer(c, data: CertificateData) -> None:
    line_y = 105
    line_w = 180

    # --- signature block (left) ---
    left_x = 95
    c.setStrokeColor(INK)
    c.setLineWidth(0.8)
    c.line(left_x, line_y, left_x + line_w, line_y)
    sig_name = data.signatory_name or "Authorised Signatory"
    sig_size = _fit_size(sig_name, "Helvetica-Bold", 10.5, 7, line_w)
    c.setFont("Helvetica-Bold", sig_size)
    c.setFillColor(INK)
    c.drawCentredString(left_x + line_w / 2, line_y - 15, sig_name)
    if data.signatory_title:
        title_size = _fit_size(data.signatory_title, "Helvetica", 9, 6.5, line_w)
        c.setFont("Helvetica", title_size)
        c.setFillColor(MUTED)
        c.drawCentredString(left_x + line_w / 2, line_y - 28, data.signatory_title)

    # --- date block (right) ---
    right_x = PAGE_W - 95 - line_w
    c.setStrokeColor(INK)
    c.line(right_x, line_y, right_x + line_w, line_y)
    date_str = format_issue_date(data.issue_date)
    c.setFont("Helvetica", 10)
    c.setFillColor(INK)
    c.drawCentredString(right_x + line_w / 2, line_y - 15, date_str)
    c.setFont("Helvetica", 9)
    c.setFillColor(MUTED)
    c.drawCentredString(right_x + line_w / 2, line_y - 28, "Date of issue")

    # --- seal (centre): gold rings, star and issuer initials ---
    cx, cy, r = PAGE_W / 2, 118, 36
    c.setStrokeColor(GOLD)
    c.setLineWidth(2.2)
    c.circle(cx, cy, r)
    c.setLineWidth(0.8)
    c.circle(cx, cy, r - 5)
    _star(c, cx, cy + 9, 8, GOLD)
    initials = "".join(w[0] for w in data.issuer_name.split()[:3] if w).upper() or "★"
    try:
        initials.encode("cp1252")
    except UnicodeEncodeError:
        initials = "CERT"
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(NAVY)
    c.drawCentredString(cx, cy - 14, initials)

    # --- certificate id (audit trail; printed small at the bottom) ---
    _center(c, f"Certificate ID: {data.certificate_uid}", 48, "Courier", 8, MUTED)
