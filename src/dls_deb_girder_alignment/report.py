"""
Report generation — PDF for humans, JSON for machines.

No asset-DB API yet, so both land in a local folder. The JSON sibling is what the
asset database should ingest when an API exists; the PDF is the signed record.

A partial report (session stopped mid-alignment) is produced on request and is
watermarked INCOMPLETE so it can never be mistaken for a finished alignment.
"""

# reportlab 5.0.1's bundled stubs mistype the shape keyword arguments: Rect,
# Line, Circle and String all accept fillColor/strokeColor/strokeWidth at
# runtime, which is what the drawing code below relies on.
# pyright: reportArgumentType=false
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

import numpy as np
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm as MM
from reportlab.pdfgen import canvas as _canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Table,
    TableStyle,
)

from . import geometry as G

ACCENT = colors.HexColor("#1f6f8b")
GREY = colors.HexColor("#555f6a")
LIGHT = colors.HexColor("#eef2f5")
BAD = colors.HexColor("#b3261e")
WARN = colors.HexColor("#9a6700")
OK = colors.HexColor("#1e6b3a")


def _pose_disp(m, err, p):
    """Correction still required at point p, from the fitted pose error."""
    v = -m.to_master(err)
    t, th = v[:3], v[3:]
    return t + np.cross(th, np.asarray(p, float) - m.p0)


def _views_drawing(session, width, err=None, label="required correction"):
    """Plan + end views drawn with ReportLab's own primitives.

    Deliberately NOT matplotlib: an optional import that fails silently is how
    these views vanished from the report in the first place. This needs nothing
    beyond reportlab, so it cannot go missing.

    Both views are in the JACK frame (girder horizontal in plan, looking
    downstream in end view), matching the on-screen views. Arrow length is scaled
    automatically so the largest correction reads clearly at any error magnitude,
    and the resulting factor is printed so nothing is mistaken for to-scale.

    ``err`` defaults to the initial (required-correction) pose; pass
    ``session.current_err`` for an as-left view instead, with a matching
    ``label`` for the in-drawing caption.
    """
    from reportlab.graphics.shapes import (
        Circle,
        Drawing,
        Group,
        Line,
        Polygon,
        Rect,
        String,
    )

    m = session.machine
    err = err if err is not None else (session.initial_err or session.current_err)
    # Draw the correction to the INTENDED target, not true zero: with a heave
    # pre-set in effect the girder is deliberately left heave_bias_mm off raw
    # nominal, so a correctly-positioned girder must show as no correction
    # needed here, not as still being short by the bias (see assess()'s
    # `target` param, used the same way for the pose table above).
    bias = session.heave_bias_mm or 0.0
    if err and bias:
        err = dict(err)
        err["heave"] = err.get("heave", 0.0) - bias
    jx, jz, p0 = m.jx, m.jz, m.p0

    def along(p):
        return float((np.asarray(p, float) - p0) @ jz)

    def across(p):
        return float((np.asarray(p, float) - p0) @ jx)

    fids = list(m.fids.values())
    C_LINE = colors.HexColor("#94a6b4")
    C_BODY = colors.HexColor("#eaf1f6")
    C_VEC = colors.HexColor("#1f6f8b")
    C_SURGE = colors.HexColor("#1e6b3a")
    C_SWAY = colors.HexColor("#9a6700")
    C_TXT = colors.HexColor("#55636e")

    disp = {i: _pose_disp(m, err, f) for i, f in enumerate(fids)} if err else {}
    maxd = max((float(np.linalg.norm(d)) for d in disp.values()), default=0.0)

    # Size the canvas from the content so there is no dead space in the PDF.
    pad = 10 * MM
    A0 = [along(p) for p in fids] + [along(b["p"]) for b in m.bearings]
    C0 = [across(p) for p in fids] + [across(b["p"]) for b in m.bearings]
    aMin, aMax = min(A0) - 420, max(A0) + 420
    cMin, cMax = min(C0) - 260, max(C0) + 260
    s = (width - 2 * pad) / (aMax - aMin)
    PLAN_H = (cMax - cMin) * s + 9 * MM  # + label strip
    vens0 = [e["p"] for e in m.venc]
    AE0 = [across(p) for p in fids] + [across(v) for v in vens0]
    YE0 = (
        [float(p[1]) for p in fids]
        + [float(v[1]) for v in vens0]
        + [float(e2["p"][1]) for e2 in m.henc]
    )
    aMinE, aMaxE = min(AE0) - 260, max(AE0) + 260
    yMinE, yMaxE = min(YE0) - 130, max(YE0) + 110
    ew = 78 * MM
    se = ew / (aMaxE - aMinE)
    END_H = (yMaxE - yMinE) * se + 8 * MM
    # GAP/CAP set the buffer between the two views and around each caption -
    # generous on purpose: a large correction's auto-scaled arrow (below) can
    # reach close to its view's edge, and a tight caption strip is exactly
    # what let it overlap the caption text underneath.
    GAP, CAP = 7 * MM, 5 * MM
    d = Drawing(width, PLAN_H + GAP + END_H + CAP)

    def arrow(g, x1, y1, x2, y2, col, w=0.9, hl=4.0):
        g.add(Line(x1, y1, x2, y2, strokeColor=col, strokeWidth=w))
        a = math.atan2(y2 - y1, x2 - x1)
        g.add(
            Polygon(
                [
                    x2,
                    y2,
                    x2 - hl * math.cos(a - 0.40),
                    y2 - hl * math.sin(a - 0.40),
                    x2 - hl * math.cos(a + 0.40),
                    y2 - hl * math.sin(a + 0.40),
                ],
                fillColor=col,
                strokeColor=col,
            )
        )

    # ---------------- PLAN (upper) ----------------
    g = Group()
    ox0 = pad
    oy0 = END_H + GAP + CAP + 7 * MM

    def px(p):
        return (ox0 + (along(p) - aMin) * s, oy0 + (across(p) - cMin) * s)

    # Arrow length is auto-scaled: the largest correction becomes a fixed fraction
    # of the girder half-width, so it reads clearly whether the error is 5 mm or
    # 20 um and never sprawls across the page. Kept comfortably under half so
    # the worst-case arrow, from a point already near the box edge, still lands
    # inside the box rather than reaching the caption below it.
    ascale = (0.34 * (cMax - cMin) * s / maxd) if maxd > 1e-9 else 0.0

    fa = [along(p) for p in fids]
    fc = [across(p) for p in fids]
    bx0, by0 = ox0 + (min(fa) - 170 - aMin) * s, oy0 + (min(fc) - 150 - cMin) * s
    bx1, by1 = ox0 + (max(fa) + 170 - aMin) * s, oy0 + (max(fc) + 150 - cMin) * s
    g.add(
        Rect(
            bx0,
            by0,
            bx1 - bx0,
            by1 - by0,
            fillColor=C_BODY,
            strokeColor=C_LINE,
            strokeWidth=0.7,
        )
    )
    g.add(
        String(
            bx0 + 4, by1 - 8, f"GIRDER {session.gtype}", fontSize=5.5, fillColor=C_TXT
        )
    )
    g.add(String(bx0 + 4, by0 + 3, "U/S", fontSize=5, fillColor=C_TXT))
    g.add(String(bx1 - 15, by0 + 3, "D/S", fontSize=5, fillColor=C_TXT))
    g.add(
        String(
            ox0,
            by1 + 12,
            "\u25c4 master CSYS upstream",
            fontSize=5,
            fillColor=colors.HexColor("#7a6fbf"),
        )
    )
    g.add(
        String(
            ox0 + (aMax - aMin) * s - 34,
            by1 + 12,
            "beam +Z \u25ba",
            fontSize=5,
            fillColor=C_VEC,
        )
    )

    ox, oy = px(p0)
    L = 22
    g.add(Line(ox, oy, ox + L, oy, strokeColor=C_VEC, strokeWidth=1.0))
    g.add(String(ox + L + 2, oy - 1.6, "jack-Z", fontSize=4.5, fillColor=C_VEC))
    g.add(Line(ox, oy, ox, oy + L, strokeColor=C_SWAY, strokeWidth=1.0))
    g.add(String(ox - 7, oy + L + 2, "jack-X", fontSize=4.5, fillColor=C_SWAY))
    g.add(Circle(ox, oy, 1.5, fillColor=colors.black, strokeColor=None))

    for b in m.bearings:
        x, y = px(b["p"])
        g.add(
            Circle(
                x,
                y,
                3.6,
                fillColor=colors.HexColor("#cfe6ef"),
                strokeColor=C_VEC,
                strokeWidth=1.0,
            )
        )
        g.add(
            String(
                x - 8,
                y + 5.5,
                b["id"].replace("J_", "").replace("_", "\u00b7"),
                fontSize=4.0,
                fillColor=C_TXT,
            )
        )
    for e2 in m.henc:
        x, y = px(e2["p"])
        col = C_SURGE if e2["kind"] == "surge" else C_SWAY
        g.add(
            Rect(
                x - 1.9,
                y - 1.9,
                3.8,
                3.8,
                fillColor=None,
                strokeColor=col,
                strokeWidth=0.9,
            )
        )

    for i, f in enumerate(fids):
        x, y = px(f)
        if err:
            dd = disp[i]
            dx, dy2 = float(dd @ jz) * ascale, float(dd @ jx) * ascale
            if math.hypot(dx, dy2) > 1.2:
                arrow(g, x, y, x + dx, y + dy2, C_VEC)
            vy = float(dd[1])
            g.add(
                String(
                    x - 7,
                    y - 8,
                    ("\u25b2" if vy >= 0 else "\u25bc") + f"{abs(vy):.2f}",
                    fontSize=4.1,
                    fillColor=C_SURGE if vy < 0 else C_SWAY,
                )
            )
        g.add(Circle(x, y, 1.4, fillColor=C_VEC if err else C_LINE, strokeColor=None))
    g.add(
        String(
            ox0,
            oy0 - 5.5 * MM,
            "PLAN VIEW \u2014 jack frame, girder horizontal. Arrows = "
            f"{label} at each survey point "
            f"(\u00d7{ascale / max(s, 1e-9):,.0f} "
            "vs geometry); \u25b2\u25bc = vertical mm.",
            fontSize=5.2,
            fillColor=C_TXT,
        )
    )
    d.add(g)

    # ---------------- END (lower, centred) ----------------
    ge = Group()
    vens = vens0
    exo = (width - (aMaxE - aMinE) * se) / 2
    eyo = CAP + 5.5 * MM

    def pxe(p):
        return (exo + (across(p) - aMinE) * se, eyo + (float(p[1]) - yMinE) * se)

    faE = [across(p) for p in fids]
    topY = max(float(p[1]) for p in fids)
    botY = min(float(v[1]) for v in vens)
    ex0 = exo + (min(faE) - 110 - aMinE) * se
    ex1 = exo + (max(faE) + 110 - aMinE) * se
    ey0 = eyo + (botY - yMinE) * se
    ey1 = eyo + (topY - yMinE) * se
    ge.add(
        Rect(
            ex0,
            ey0,
            ex1 - ex0,
            ey1 - ey0,
            fillColor=C_BODY,
            strokeColor=C_LINE,
            strokeWidth=0.7,
        )
    )
    gx, gy = exo + 4, eyo + (yMaxE - yMinE) * se - 20
    ge.add(Line(gx, gy, gx + 14, gy, strokeColor=C_SWAY, strokeWidth=1.0))
    ge.add(String(gx + 16, gy - 1.6, "jack-X outboard", fontSize=4.3, fillColor=C_SWAY))
    ge.add(Line(gx, gy, gx, gy + 14, strokeColor=C_SURGE, strokeWidth=1.0))
    ge.add(String(gx - 2, gy + 16, "Y", fontSize=4.3, fillColor=C_SURGE))
    for v in vens:
        x, y = pxe(v)
        ge.add(Circle(x, y, 1.0, fillColor=C_LINE, strokeColor=None))
    for e2 in m.henc:
        x, y = pxe(np.asarray(e2["p"], float))
        col = C_SURGE if e2["kind"] == "surge" else C_SWAY
        ge.add(
            Rect(
                x - 1.6,
                y - 1.6,
                3.2,
                3.2,
                fillColor=None,
                strokeColor=col,
                strokeWidth=0.8,
            )
        )
    # In this projection the fiducials collapse onto a handful of transverse
    # positions, so nine overlapping arrows would be unreadable. Group by across
    # position and draw the mean correction once per distinct location.
    asc_e = (0.19 * (yMaxE - yMinE) * se / maxd) if maxd > 1e-9 else 0.0
    groups = {}
    for i, f in enumerate(fids):
        key = round(across(f) / 5.0)
        groups.setdefault(key, []).append(i)
    for idxs in groups.values():
        f0 = fids[idxs[0]]
        x, y = pxe(f0)
        if err:
            dd = np.mean([disp[i] for i in idxs], axis=0)
            dx, dy2 = float(dd @ jx) * asc_e, float(dd[1]) * asc_e
            if math.hypot(dx, dy2) > 1.2:
                arrow(ge, x, y, x + dx, y + dy2, C_VEC, w=0.9, hl=3.6)
            ge.add(
                String(
                    x - 9, y + 5, f"{float(dd[1]):+.2f}Y", fontSize=4.1, fillColor=C_TXT
                )
            )
        ge.add(Circle(x, y, 1.5, fillColor=C_VEC if err else C_LINE, strokeColor=None))
        ge.add(
            String(
                x - 10,
                y - 9,
                f"{across(f0):+.0f} ({len(idxs)} pts)",
                fontSize=3.9,
                fillColor=C_LINE,
            )
        )
    # Left-justified at the same x as the PLAN VIEW caption below (`pad`, the
    # drawing's own left margin) - not `exo`, which is the END view's own
    # centred sub-box origin and would centre this caption within the page
    # rather than align it with the other one.
    ge.add(
        String(
            pad,
            2.2 * MM,
            "END VIEW \u2014 looking along the jack-Z (beam) axis.",
            fontSize=5.0,
            fillColor=C_TXT,
        )
    )
    d.add(ge)
    return d


def _styles():
    ss = getSampleStyleSheet()
    ss.add(
        ParagraphStyle(
            "H", fontName="Helvetica-Bold", fontSize=15, textColor=ACCENT, spaceAfter=2
        )
    )
    ss.add(
        ParagraphStyle(
            "Sub", fontName="Helvetica", fontSize=8.5, textColor=GREY, spaceAfter=8
        )
    )
    ss.add(
        ParagraphStyle(
            "H2",
            fontName="Helvetica-Bold",
            fontSize=10.5,
            textColor=ACCENT,
            spaceBefore=10,
            spaceAfter=4,
        )
    )
    ss.add(ParagraphStyle("Body", fontName="Helvetica", fontSize=8.5, leading=11))
    ss.add(
        ParagraphStyle(
            "Small", fontName="Helvetica", fontSize=7.5, textColor=GREY, leading=9.5
        )
    )
    ss.add(
        ParagraphStyle(
            "Warn",
            fontName="Helvetica-Bold",
            fontSize=9,
            textColor=BAD,
            alignment=TA_CENTER,
            spaceAfter=6,
        )
    )
    ss.add(ParagraphStyle("Cell", fontName="Helvetica", fontSize=7.6, leading=9))
    return ss


def _cell(text, ss) -> Paragraph:
    """A wrapping table cell.

    Plain strings in a reportlab Table only break on a literal "\\n" - a long
    filename, step title or free-text reason just overruns into the next
    column. Use this for any column that can hold generated or free text.
    """
    return Paragraph(_xml_escape(str(text)), ss["Cell"])


def _tbl(data, widths, style_extra=None, header=True):
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    st = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 7.6),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.black),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c8d0d8")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    if header:
        st += [
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.6),
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ]
    t.setStyle(TableStyle(st + (style_extra or [])))
    return t


def _fmt(v, nd=3):
    return "—" if v is None else f"{v:+.{nd}f}"


class _Doc(BaseDocTemplate):
    def __init__(self, path, session, partial, **kw):
        super().__init__(
            path,
            pagesize=A4,
            leftMargin=16 * MM,
            rightMargin=16 * MM,
            topMargin=14 * MM,
            bottomMargin=16 * MM,
            **kw,
        )
        self.session = session
        self.partial = partial
        frame = Frame(
            self.leftMargin, self.bottomMargin, self.width, self.height, id="f"
        )
        self.addPageTemplates(
            [PageTemplate(id="p", frames=[frame], onPage=self._decorate)]
        )

    def _decorate(self, canv, doc):
        canv.saveState()
        s = self.session
        canv.setFont("Helvetica", 7)
        canv.setFillColor(GREY)
        canv.drawString(
            16 * MM,
            9 * MM,
            f"Girder {s.serial} ({s.gtype}) · session {s.id} · operator {s.operator}",
        )
        # Page number itself is drawn by _NumberedCanvas.save(), once the
        # total page count is known - drawing "Page X" here (doc.page alone)
        # would need a second value this single pass through the flowables
        # never has.
        canv.setStrokeColor(colors.HexColor("#c8d0d8"))
        canv.line(16 * MM, 12 * MM, A4[0] - 16 * MM, 12 * MM)
        if self.partial:
            canv.saveState()
            canv.setFont("Helvetica-Bold", 52)
            canv.setFillColor(colors.Color(0.85, 0.15, 0.12, alpha=0.10))
            canv.translate(A4[0] / 2, A4[1] / 2)
            canv.rotate(38)
            canv.drawCentredString(0, 0, "INCOMPLETE")
            canv.restoreState()
        canv.restoreState()


class _NumberedCanvas(_canvas.Canvas):
    """Buffers pages so the footer can print "Page X of Y" - the total page
    count isn't known until every flowable has been laid out, which is only
    true once the whole document has already been drawn once.
    """

    def __init__(self, *args, **kwargs):
        _canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states: list[dict] = []

    def showPage(self):  # noqa: N802 - overrides reportlab's Canvas.showPage
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()  # pyright: ignore[reportAttributeAccessIssue]

    def save(self):
        total = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.setFont("Helvetica", 7)
            self.setFillColor(GREY)
            page_no = self._pageNumber  # pyright: ignore[reportAttributeAccessIssue]
            self.drawRightString(A4[0] - 16 * MM, 9 * MM, f"Page {page_no} of {total}")
            _canvas.Canvas.showPage(self)
        _canvas.Canvas.save(self)


def build_pdf(session, out_dir: Path, partial: bool = False) -> Path:
    ss = _styles()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = "PARTIAL" if partial else "FINAL"
    path = out_dir / f"girder_{session.serial}_{session.gtype}_{stamp}_{suffix}.pdf"

    st = []
    st.append(Paragraph("Girder Alignment Report", ss["H"]))
    if partial:
        st.append(
            Paragraph(
                "THIS ALIGNMENT WAS NOT COMPLETED — DO NOT USE AS AN AS-LEFT RECORD",
                ss["Warn"],
            )
        )

    # -- identity ---------------------------------------------------------
    a = session.state().get("assessment") or {}
    ident_tbl = _tbl(
        [
            ["Girder serial", session.serial, "Operator", session.operator],
            ["Girder type", session.gtype, "Session id", session.id],
            ["Started", session.created, "Status", session.status],
            [
                "Geometry revision",
                session.geometry_revision,
                "Software",
                session.software_version,
            ],
            [
                "Bay",
                session.bay or "—",
                "Temperature",
                "—" if session.temperature is None else f"{session.temperature:.1f} °C",
            ],
            [
                "Iterations",
                str(session.iteration),
                "Moves",
                str(len(session.moves)),
            ],
            ["Girder state", _girder_state(session), "", ""],
        ],
        [30 * MM, 52 * MM, 30 * MM, 52 * MM],
        header=False,
    )
    st.append(KeepTogether([Paragraph("Identification", ss["H2"]), ident_tbl]))

    # -- outcome ----------------------------------------------------------
    rows = [
        ["Axis", "Unit", "Tolerance", "Initial error", "As-left", "% of tol", "Status"]
    ]
    style_extra = []
    for i, (key, label, unit, _d) in enumerate(G.AXES, start=1):
        tol = session.tol.get(key)
        ini = (session.initial_err or {}).get(key)
        cur = (session.current_err or {}).get(key)
        ax = (a.get("axes") or {}).get(key, {})
        frac = ax.get("frac")
        status = ax.get("status", "—")
        rows.append(
            [
                label,
                unit,
                f"±{tol:g}",
                _fmt(ini),
                _fmt(cur),
                "—" if frac is None else f"{frac * 100:.0f}%",
                status.upper(),
            ]
        )
        col = {"ok": OK, "warn": WARN, "bad": BAD}.get(status)
        if col:
            style_extra.append(("TEXTCOLOR", (6, i), (6, i), col))
            style_extra.append(("FONT", (6, i), (6, i), "Helvetica-Bold", 7.6))
    pose_tbl = _tbl(
        rows,
        [24 * MM, 14 * MM, 20 * MM, 26 * MM, 26 * MM, 20 * MM, 22 * MM],
        style_extra,
    )
    st.append(
        KeepTogether([Paragraph("Pose — initial vs as-left", ss["H2"]), pose_tbl])
    )
    # "As-left" only ever comes from a survey upload, never from a move — if
    # the last move happened after the last survey (or there's no survey at
    # all yet), the "As-left" column above is really the PRE-move pose, not
    # a confirmed as-left result. Flag it explicitly rather than let it look
    # like a stale-looking match is a bug.
    last_survey_t = session.surveys[-1]["t"] if session.surveys else None
    last_move_t = session.moves[-1]["t"] if session.moves else None
    if last_move_t and (last_survey_t is None or last_survey_t < last_move_t):
        st.append(
            Paragraph(
                "! No survey has been taken since the last move — “As-left” "
                "above is the pose from BEFORE that move, not a confirmed "
                "result. Re-survey to confirm the as-left pose.",
                ss["Warn"],
            )
        )

    # -- plan + end views -------------------------------------------------
    st.append(
        KeepTogether(
            [
                Paragraph("Required correction — plan and end views", ss["H2"]),
                _views_drawing(session, 170 * MM, err=session.initial_err),
                Paragraph(
                    "Arrows are the correction at each survey point (measured → "
                    "nominal). Both views are in the jack frame: the plan view "
                    "puts the girder horizontal, the end view looks downstream "
                    "along the beam. In-plane arrows are exaggerated to be "
                    "visible; in the plan view the vertical component is "
                    "labelled numerically in mm.",
                    ss["Small"],
                ),
            ]
        )
    )

    if session.current_err:
        st.append(
            KeepTogether(
                [
                    Paragraph("As-left — plan and end views", ss["H2"]),
                    _views_drawing(
                        session,
                        170 * MM,
                        err=session.current_err,
                        label="as-left residual",
                    ),
                    Paragraph(
                        "Same projection and scaling convention as the required-"
                        "correction views above, but showing what's left after "
                        "the recorded moves (as-left survey → nominal), not the "
                        "starting error.",
                        ss["Small"],
                    ),
                ]
            )
        )

    # -- surveys ----------------------------------------------------------
    st.append(Paragraph("Survey history", ss["H2"]))
    rows = [
        [
            "#",
            "Time (UTC)",
            "File",
            "Pts",
            "RMS mm",
            "Max mm",
            "Roll",
            "Sway",
            "Heave",
            "Yaw",
            "Pitch",
            "Surge",
        ]
    ]
    for s in session.surveys:
        e = s["err"]
        rows.append(
            [
                str(s["iteration"]),
                s["t"].replace("T", " ")[:19],
                _cell(s.get("filename") or s.get("source", "—"), ss),
                f"{s['fit']['n']}",
                f"{s['fit']['rms']:.4f}",
                f"{s['fit']['max']:.4f}",
                _fmt(e["roll"]),
                _fmt(e["sway"]),
                _fmt(e["heave"]),
                _fmt(e["yaw"]),
                _fmt(e["pitch"]),
                _fmt(e["surge"]),
            ]
        )
    if len(rows) == 1:
        rows.append(["—"] * 12)
    st.append(
        _tbl(
            rows,
            [
                7 * MM,
                28 * MM,
                22 * MM,
                9 * MM,
                14 * MM,
                14 * MM,
                13 * MM,
                13 * MM,
                13 * MM,
                13 * MM,
                13 * MM,
                13 * MM,
            ],
        )
    )
    warns = [w for s in session.surveys for w in s["fit"].get("warnings", [])]
    if warns:
        st.append(
            Paragraph(
                "Survey warnings: " + "; ".join(dict.fromkeys(warns)), ss["Small"]
            )
        )
    comments = [
        (s["iteration"], s["comment"]) for s in session.surveys if s.get("comment")
    ]
    if comments:
        st.append(
            Paragraph(
                "Operator comments: "
                + "; ".join(f"#{i} — {_xml_escape(c)}" for i, c in comments),
                ss["Small"],
            )
        )

    # -- moves ------------------------------------------------------------
    st.append(Paragraph("Moves — target vs achieved", ss["H2"]))
    rows = [
        [
            "Iter",
            "Step",
            "Encoder",
            "Target mm",
            "Achieved mm",
            "Error mm",
            "WARP",
            "STRETCH",
            "Gate",
        ]
    ]
    extra = []
    r = 1
    any_reopened = False
    for mv in session.moves:
        reopened = bool(mv.get("reopened"))
        any_reopened = any_reopened or reopened
        row_start = r
        first = True
        title = mv["step"] + (" [REOPENED — redone below]" if reopened else "")
        status_parts = []
        if mv["override"]:
            status_parts.append("OVERRIDE")
        if reopened:
            status_parts.append("REOPENED")
        status = " + ".join(status_parts) if status_parts else "pass"
        for eid, tgt in mv["targets"].items():
            ach = (mv["achieved"] or {}).get(eid)
            err = (mv["errors"] or {}).get(eid)
            rows.append(
                [
                    str(mv["iteration"]) if first else "",
                    _cell(title, ss) if first else "",
                    eid,
                    _fmt(tgt),
                    _fmt(ach),
                    _fmt(err, 4),
                    f"{mv['warp']:+.4f}" if first else "",
                    f"{mv['stretch']:+.4f}" if first else "",
                    status if first else "",
                ]
            )
            if first and mv["override"]:
                extra.append(("TEXTCOLOR", (8, r), (8, r), BAD))
                extra.append(("FONT", (8, r), (8, r), "Helvetica-Bold", 7.6))
            if first and reopened:
                extra.append(("TEXTCOLOR", (8, r), (8, r), WARN))
                extra.append(("FONT", (8, r), (8, r), "Helvetica-Bold", 7.6))
            first = False
            r += 1
        # The other, monitored encoders — not this step's own target, but
        # still read at the moment it completed (all_encoders is the full
        # 8-encoder reading; targets is just the 1-2 this step drove).
        for eid in G.ENCODER_IDS:
            if eid in mv["targets"]:
                continue
            ach = (mv.get("all_encoders") or {}).get(eid)
            rows.append(
                ["", "", _cell(f"{eid} (monitor)", ss), "—", _fmt(ach), "—", "", "", ""]
            )
            extra.append(("TEXTCOLOR", (2, r), (5, r), GREY))
            r += 1
        if mv.get("comment"):
            rows.append(
                ["", "", _cell(f"Comment: {mv['comment']}", ss), "", "", "", "", "", ""]
            )
            extra.append(("TEXTCOLOR", (2, r), (2, r), ACCENT))
            r += 1
        if reopened:
            extra.append(
                ("BACKGROUND", (0, row_start), (-1, r - 1), colors.HexColor("#fdf0e0"))
            )
    if len(rows) == 1:
        rows.append(["—"] * 9)
    st.append(
        _tbl(
            rows,
            [
                11 * MM,
                30 * MM,
                24 * MM,
                21 * MM,
                22 * MM,
                20 * MM,
                17 * MM,
                18 * MM,
                19 * MM,
            ],
            extra,
        )
    )
    moves_note = (
        "Achieved values are encoder readings relative to the datum "
        "captured automatically when each move group opened. Only the last "
        "pair moved lands on final targets; earlier "
        "pairs stop at a compensated intermediate value "
        f"(cross-shift ≈ {G.cross_shift_ratio(session.machine):.4f} mm per mm of "
        "far-pair travel). Rows marked “(monitor)” are the other, "
        "un-targeted encoders' readings at the same moment — recorded "
        "even though this step didn't drive them."
    )
    if any_reopened:
        moves_note += (
            " Highlighted rows were REOPENED — the operator went back to that "
            "step after confirming it complete, so it was redone; the later "
            "entry for the same step is the one that stands."
        )
    st.append(Paragraph(moves_note, ss["Small"]))

    # -- overrides --------------------------------------------------------
    if session.overrides:
        st.append(Paragraph("Tolerance gate overrides", ss["H2"]))
        rows = [["Time (UTC)", "Step", "By", "Worst err mm", "Gate mm", "Reason"]]
        for o in session.overrides:
            rows.append(
                [
                    o["t"].replace("T", " ")[:19],
                    _cell(o["step"], ss),
                    o["by"],
                    f"{o['worst_error']:.4f}",
                    f"{o['gate_mm']:.4f}",
                    _cell(o["reason"] or "—", ss),
                ]
            )
        st.append(_tbl(rows, [30 * MM, 28 * MM, 24 * MM, 22 * MM, 18 * MM, 56 * MM]))

    # -- model validation -------------------------------------------------
    checks = [m for m in session.moves if m.get("model_check")]
    if checks:
        st.append(Paragraph("Model validation — predicted vs achieved", ss["H2"]))
        rows = [["Steps since last survey", "Axis", "Predicted", "Actual", "Residual"]]
        any_warp = False
        for m in checks:
            mc = m["model_check"]
            label = ", ".join(mc.get("steps") or [m["step"]])
            first = True
            for k, _l, _u, _d in G.AXES:
                rows.append(
                    [
                        _cell(label, ss) if first else "",
                        k,
                        _fmt(mc["predicted"].get(k)),
                        _fmt(mc["actual"].get(k)),
                        _fmt(mc["residual"].get(k)),
                    ]
                )
                first = False
            # "warp" isn't in AXES (it's a parasitic mode, not a correctable
            # DOF) and is only ever predicted - not just echoed back as its
            # own residual - when the girder type has FEA compliance data;
            # see predicted_pose_change's docstring. Shown separately, and
            # only when it was genuinely predicted, so this table never
            # implies a validation that didn't happen.
            if "warp" in mc["predicted"]:
                any_warp = True
                rows.append(
                    [
                        "",
                        "warp",
                        _fmt(mc["predicted"].get("warp"), 4),
                        _fmt(mc["actual"].get("warp"), 4),
                        _fmt(mc["residual"].get("warp"), 4),
                    ]
                )
        st.append(_tbl(rows, [34 * MM, 24 * MM, 34 * MM, 34 * MM, 34 * MM]))
        warp_note = (
            ' "warp" rows recover the checkerboard amplitude the same way '
            "from achieved encoder deltas as the 6 rigid axes above - a "
            "consistent residual here across many sessions is exactly the "
            "signal that the stored FEA compliance gain needs revising."
            if any_warp
            else ""
        )
        st.append(
            Paragraph(
                "Achieved encoder deltas, pushed through the Jacobian, summed "
                "across every step since the previous survey - not just the "
                "last one, or a heave correction made two steps before a "
                "final surge move would show up entirely as residual - and "
                "compared with the next survey. Large residuals indicate the "
                "geometric model or the bearing pivot assumption needs review."
                + warp_note,
                ss["Small"],
            )
        )

    # -- survey data --------------------------------------------------------
    # Every survey, not just the last: an alignment is normally a handful of
    # iterations, so this stays a short, readable list rather than needing
    # pagination or a "changed since last time" summary.
    if session.surveys:
        for sv in session.surveys:
            rows = [["Reference", "X", "Y", "Z", "Residual mm"]]
            for name, p in sv["matched"].items():
                rows.append(
                    [
                        name,
                        f"{p[0]:.3f}",
                        f"{p[1]:.3f}",
                        f"{p[2]:.3f}",
                        f"{sv['fit']['residuals'].get(name, 0):.4f}",
                    ]
                )
            sv_group = [
                Paragraph(
                    f"Uploaded survey data — iteration {sv['iteration']}", ss["H2"]
                ),
                _tbl(rows, [30 * MM, 32 * MM, 32 * MM, 32 * MM, 30 * MM]),
            ]
            st.append(KeepTogether(sv_group))
        st.append(
            Paragraph(
                "Points are matched to reference fiducials by proximity (gate "
                f"{G.PROXIMITY_GATE_MM:g} mm), not by name — names repeat "
                "between girder types. Residual is the fit residual (after "
                "removing the best-fit rigid pose), not the raw offset from "
                "nominal — see the summary below for that.",
                ss["Small"],
            )
        )

        # -- final survey vs nominal, raw (not fit-residual) ---------------
        # Measured against the INTENDED target, not raw nominal: with a
        # heave pre-set in effect, the girder is deliberately left
        # heave_bias_mm off true nominal in Y, so a correctly-positioned
        # girder must show as zero here, not as "off by the bias".
        last = session.surveys[-1]
        m = session.machine
        bias = session.heave_bias_mm or 0.0
        target_offset = np.array([0.0, bias, 0.0])
        names = [name for name in last["matched"] if name in m.fids]
        deltas = [
            np.asarray(last["matched"][name], float)
            - np.asarray(m.fids[name], float)
            - target_offset
            for name in names
        ]
        if deltas:
            arr = np.array(deltas)
            mag = np.linalg.norm(arr, axis=1)
            final_note = (
                "Raw measured − target per point, before any rigid-pose fit "
                "— this is how far the girder actually was from where it was "
                "meant to be, not how well the fit explains it."
                if not bias
                else (
                    "Raw measured − target per point (target = nominal, with "
                    f"the {bias:+.3f} mm heave pre-set applied in Y), before "
                    "any rigid-pose fit — this is how far the girder actually "
                    "was from where it was meant to be, not how well the fit "
                    "explains it."
                )
            )

            # Plain ASCII column headers - reportlab's base Helvetica is
            # WinAnsi-encoded and has no glyph for U+0394 (GREEK CAPITAL
            # DELTA), unlike the em dash/plus-minus/multiply signs used
            # elsewhere in this file, which WinAnsi does cover.
            point_rows = [["Reference", "dX mm", "dY mm", "dZ mm", "3D delta mm"]]
            for name, row, m3 in zip(names, arr, mag, strict=True):
                point_rows.append(
                    [
                        name,
                        f"{row[0]:+.3f}",
                        f"{row[1]:+.3f}",
                        f"{row[2]:+.3f}",
                        f"{m3:.3f}",
                    ]
                )
            st.append(
                KeepTogether(
                    [
                        Paragraph(
                            f"Final survey vs target — per point (iteration "
                            f"{last['iteration']})",
                            ss["H2"],
                        ),
                        _tbl(point_rows, [30 * MM, 32 * MM, 32 * MM, 32 * MM, 30 * MM]),
                        Paragraph(final_note, ss["Small"]),
                    ]
                )
            )

            summary_rows = [["Axis", "Mean mm", "RMS mm", "Max |·| mm"]]
            for i, axis in enumerate("XYZ"):
                col = arr[:, i]
                summary_rows.append(
                    [
                        axis,
                        f"{np.mean(col):+.3f}",
                        f"{math.sqrt(np.mean(col**2)):.3f}",
                        f"{np.max(np.abs(col)):.3f}",
                    ]
                )
            summary_rows.append(
                [
                    "Magnitude",
                    "—",
                    f"{math.sqrt(np.mean(mag**2)):.3f}",
                    f"{np.max(mag):.3f}",
                ]
            )
            st.append(
                KeepTogether(
                    [
                        Paragraph(
                            f"Final survey vs target — summary (iteration "
                            f"{last['iteration']})",
                            ss["H2"],
                        ),
                        _tbl(summary_rows, [30 * MM, 30 * MM, 30 * MM, 30 * MM]),
                    ]
                )
            )

    _Doc(str(path), session, partial).build(st, canvasmaker=_NumberedCanvas)
    return path


def _girder_state(session) -> str:
    # The session only records the heave pre-set: the UI's "loaded" toggle
    # forces it to 0.0, "bare" takes the type's recommended pre-set.
    bias = session.heave_bias_mm or 0.0
    if bias:
        return f"Bare (heave pre-set {bias:+.3f} mm)"
    return "Loaded with magnets (no pre-set)"


def build_json(session, out_dir: Path, partial: bool = False) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = "PARTIAL" if partial else "FINAL"
    path = out_dir / f"girder_{session.serial}_{session.gtype}_{stamp}_{suffix}.json"
    d = session.to_dict()
    d["partial"] = partial
    d["girder_state"] = "loaded" if not session.heave_bias_mm else "bare"
    d["generated"] = datetime.now(UTC).isoformat(timespec="seconds")
    path.write_text(json.dumps(d, indent=1, default=str))
    return path


def build(session, out_dir: Path, partial: bool = False):
    return build_pdf(session, out_dir, partial), build_json(session, out_dir, partial)
