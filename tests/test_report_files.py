"""Report PDF/JSON pairing, and the mirror script finding the JSON sibling."""

from __future__ import annotations

import importlib.util
import itertools
from pathlib import Path

from dls_deb_girder_alignment import geometry as G
from dls_deb_girder_alignment import report as R
from dls_deb_girder_alignment.session import Session

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_mirror():
    spec = importlib.util.spec_from_file_location(
        "mirror_reports", SCRIPTS / "mirror_reports.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_pdf_and_json_share_one_stem_across_a_second_boundary(
    survey_for, tmp_path, monkeypatch
):
    """Each file once read the clock itself, so a PDF build that crossed a
    second left the .json a second later - and the mirror never copied it.
    """
    ticks = (f"20261103-0907{s:02d}" for s in itertools.count(37))
    monkeypatch.setattr(R, "_stamp", lambda: next(ticks))
    s = Session(serial="DLS01132", gtype="MS", operator="tester", bay="1")
    s.add_survey(survey_for(G.machine("MS"), {"heave": 0.1}))

    pdf, js = R.build(s, tmp_path)
    assert pdf.with_suffix("") == js.with_suffix("")


def test_mirror_looks_for_a_json_stamped_after_its_pdf():
    mirror = _load_mirror()
    names = mirror.json_candidates("girder_DLS01132_MS_20261103-090737_FINAL.pdf")
    assert names[0] == "girder_DLS01132_MS_20261103-090737_FINAL.json"
    assert "girder_DLS01132_MS_20261103-090738_FINAL.json" in names
    # Carries across minute/hour boundaries like a real clock.
    names = mirror.json_candidates("girder_X_MS_20261103-095959_PARTIAL.pdf")
    assert "girder_X_MS_20261103-100000_PARTIAL.json" in names


def test_mirror_falls_back_to_the_exact_stem_for_unknown_names():
    mirror = _load_mirror()
    assert mirror.json_candidates("something_else.pdf") == ["something_else.json"]
