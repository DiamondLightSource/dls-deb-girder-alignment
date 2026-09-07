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

import numpy as np
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm as MM
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
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


def _views_drawing(session, width):
    """Plan + end views drawn with ReportLab's own primitives.

    Deliberately NOT matplotlib: an optional import that fails silently is how
    these views vanished from the report in the first place. This needs nothing
    beyond reportlab, so it cannot go missing.

    Both views are in the JACK frame (girder horizontal in plan, looking
    downstream in end view), matching the on-screen views. Arrow length is scaled
    automatically so the largest correction reads clearly at any error magnitude,
    and the resulting factor is printed so nothing is mistaken for to-scale.
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
    err = session.initial_err or session.current_err
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
    yMinE, yMaxE = min(YE0) - 95, max(YE0) + 110
    ew = 78 * MM
    se = ew / (aMaxE - aMinE)
    END_H = (yMaxE - yMinE) * se + 8 * MM
    GAP, CAP = 5 * MM, 2 * MM
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
    # 20 um and never sprawls across the page.
    ascale = (0.42 * (cMax - cMin) * s / maxd) if maxd > 1e-9 else 0.0

    fa = [along(p) for p in fids]
    fc = [across(p) for p in fids]
    bx0, by0 = ox0 + (min(fa) - 170 - aMin) * s, oy0 + (min(fc) - 120 - cMin) * s
    bx1, by1 = ox0 + (max(fa) + 170 - aMin) * s, oy0 + (max(fc) + 120 - cMin) * s
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
            "PLAN VIEW \u2014 jack frame, girder horizontal. Arrows = required "
            f"correction at each survey point (\u00d7{ascale / max(s, 1e-9):,.0f} "
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
    asc_e = (0.24 * (yMaxE - yMinE) * se / maxd) if maxd > 1e-9 else 0.0
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
    ge.add(
        String(
            exo,
            1.2 * MM,
            "END VIEW \u2014 looking along the jack-Z (beam) axis, so the survey "
            "points from BOTH ends coincide: "
            f"{len(fids)} points collapse onto {len(groups)} transverse positions. "
            "Roll tilts the row \u00b7 heave shifts it \u00b7 sway slides it.",
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
    return ss


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
        canv.drawRightString(A4[0] - 16 * MM, 9 * MM, f"Page {doc.page}")
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


def build_pdf(session, out_dir: Path, partial: bool = False) -> Path:
    ss = _styles()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = "PARTIAL" if partial else "FINAL"
    path = out_dir / f"girder_{session.serial}_{session.gtype}_{stamp}_{suffix}.pdf"

    st = []
    st.append(Paragraph("Girder Alignment Report", ss["H"]))
    st.append(
        Paragraph(
            f"{'Partial record — alignment not completed' if partial else 'As-left record'}"  # noqa: E501
            f" · generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
            ss["Sub"],
        )
    )
    if partial:
        st.append(
            Paragraph(
                "THIS ALIGNMENT WAS NOT COMPLETED — DO NOT USE AS AN AS-LEFT RECORD",
                ss["Warn"],
            )
        )

    # -- identity ---------------------------------------------------------
    a = session.state().get("assessment") or {}
    st.append(Paragraph("Identification", ss["H2"]))
    st.append(
        _tbl(
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
                    "—"
                    if session.temperature is None
                    else f"{session.temperature:.1f} °C",
                ],
                [
                    "Iterations",
                    str(session.iteration),
                    "Moves",
                    str(len(session.moves)),
                ],
            ],
            [30 * MM, 52 * MM, 30 * MM, 52 * MM],
            header=False,
        )
    )

    # -- outcome ----------------------------------------------------------
    st.append(Paragraph("Pose — initial vs as-left", ss["H2"]))
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
    st.append(
        _tbl(
            rows,
            [24 * MM, 14 * MM, 20 * MM, 26 * MM, 26 * MM, 20 * MM, 22 * MM],
            style_extra,
        )
    )
    st.append(
        Paragraph(
            "Errors are the girder pose relative to target, in the jack frame. "
            "Angles in mrad, translations in mm. Priority order: roll, sway, heave, "
            "yaw, pitch, surge.",
            ss["Small"],
        )
    )

    # -- plan + end views -------------------------------------------------
    st.append(Paragraph("Required correction — plan and end views", ss["H2"]))
    st.append(_views_drawing(session, 170 * MM))
    st.append(
        Paragraph(
            "Arrows are the correction at each survey point (measured → nominal). Both "
            "views are in the jack frame: the plan view puts the girder "
            "horizontal, the "
            "end view looks downstream along the beam. In-plane arrows are exaggerated "
            "to be visible; in the plan view the vertical component is labelled "
            "numerically in mm.",
            ss["Small"],
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
                (s.get("filename") or s.get("source", "—"))[:18],
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
    for mv in session.moves:
        first = True
        for eid, tgt in mv["targets"].items():
            ach = (mv["achieved"] or {}).get(eid)
            err = (mv["errors"] or {}).get(eid)
            rows.append(
                [
                    str(mv["iteration"]) if first else "",
                    (mv["step"] if first else ""),
                    eid,
                    _fmt(tgt),
                    _fmt(ach),
                    _fmt(err, 4),
                    f"{mv['warp']:+.4f}" if first else "",
                    f"{mv['stretch']:+.4f}" if first else "",
                    ("OVERRIDE" if mv["override"] else "pass") if first else "",
                ]
            )
            if first and mv["override"]:
                extra.append(("TEXTCOLOR", (8, r), (8, r), BAD))
                extra.append(("FONT", (8, r), (8, r), "Helvetica-Bold", 7.6))
            first = False
            r += 1
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
    st.append(
        Paragraph(
            "Achieved values are encoder readings relative to the datum "
            "captured automatically when each move group opened. Only the last "
            "pair moved lands on final targets; earlier "
            "pairs stop at a compensated intermediate value "
            f"(cross-shift ≈ {G.cross_shift_ratio(session.machine):.4f} mm per mm of "
            "far-pair travel).",
            ss["Small"],
        )
    )

    # -- overrides --------------------------------------------------------
    if session.overrides:
        st.append(Paragraph("Tolerance gate overrides", ss["H2"]))
        rows = [["Time (UTC)", "Step", "By", "Worst err mm", "Gate mm", "Reason"]]
        for o in session.overrides:
            rows.append(
                [
                    o["t"].replace("T", " ")[:19],
                    o["step"],
                    o["by"],
                    f"{o['worst_error']:.4f}",
                    f"{o['gate_mm']:.4f}",
                    o["reason"] or "—",
                ]
            )
        st.append(_tbl(rows, [30 * MM, 28 * MM, 24 * MM, 22 * MM, 18 * MM, 56 * MM]))

    # -- model validation -------------------------------------------------
    checks = [m for m in session.moves if m.get("model_check")]
    if checks:
        st.append(Paragraph("Model validation — predicted vs achieved", ss["H2"]))
        rows = [["Step", "Axis", "Predicted", "Actual", "Residual"]]
        for m in checks:
            mc = m["model_check"]
            first = True
            for k, _l, _u, _d in G.AXES:
                rows.append(
                    [
                        m["step"] if first else "",
                        k,
                        _fmt(mc["predicted"].get(k)),
                        _fmt(mc["actual"].get(k)),
                        _fmt(mc["residual"].get(k)),
                    ]
                )
                first = False
        st.append(_tbl(rows, [34 * MM, 24 * MM, 34 * MM, 34 * MM, 34 * MM]))
        st.append(
            Paragraph(
                "Achieved encoder deltas pushed through the Jacobian and "
                "compared with the "
                "next survey. Large residuals indicate the geometric model or the "
                "bearing pivot assumption needs review.",
                ss["Small"],
            )
        )

    # -- survey data ------------------------------------------------------
    if session.surveys:
        last = session.surveys[-1]
        st.append(Paragraph("Uploaded survey data (last survey)", ss["H2"]))
        rows = [["Reference", "X", "Y", "Z", "Residual mm"]]
        for name, p in last["matched"].items():
            rows.append(
                [
                    name,
                    f"{p[0]:.3f}",
                    f"{p[1]:.3f}",
                    f"{p[2]:.3f}",
                    f"{last['fit']['residuals'].get(name, 0):.4f}",
                ]
            )
        st.append(_tbl(rows, [30 * MM, 32 * MM, 32 * MM, 32 * MM, 30 * MM]))
        st.append(
            Paragraph(
                "Points are matched to reference fiducials by proximity (gate "
                f"{G.PROXIMITY_GATE_MM:g} mm), not by name — names repeat "
                "between girder "
                "types.",
                ss["Small"],
            )
        )

    # -- sign off ---------------------------------------------------------
    st.append(Spacer(1, 8))
    st.append(
        KeepTogether(
            [
                Paragraph("Sign-off", ss["H2"]),
                _tbl(
                    [
                        ["Aligned by", session.operator, "Signature", ""],
                        ["Checked by", "", "Signature", ""],
                        ["Date", "", "Notes", session.notes or ""],
                    ],
                    [24 * MM, 50 * MM, 24 * MM, 66 * MM],
                    header=False,
                ),
            ]
        )
    )

    _Doc(str(path), session, partial).build(st)
    return path


def build_json(session, out_dir: Path, partial: bool = False) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = "PARTIAL" if partial else "FINAL"
    path = out_dir / f"girder_{session.serial}_{session.gtype}_{stamp}_{suffix}.json"
    d = session.to_dict()
    d["partial"] = partial
    d["generated"] = datetime.now(UTC).isoformat(timespec="seconds")
    path.write_text(json.dumps(d, indent=1, default=str))
    return path


def build(session, out_dir: Path, partial: bool = False):
    return build_pdf(session, out_dir, partial), build_json(session, out_dir, partial)
