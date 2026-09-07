"""Generate example laser-tracker surveys for the demo.

Takes a girder type's reference fiducials, places the girder at a known pose
error, adds tracker-scale measurement noise, and writes the result as the CSV the
tool accepts. Because the pose that went in is known, the pose the tool fits back
out can be checked against it.

    python example/surveys/make_surveys.py
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np

from dls_deb_girder_alignment import geometry as G

#: Laser-tracker repeatability over a girder-scale volume, mm (1 sigma).
TRACKER_NOISE_MM = 0.015

HERE = Path(__file__).parent


def survey(
    gtype: str, err: dict[str, float], seed: int
) -> list[tuple[str, np.ndarray]]:
    """Fiducial positions for a girder sitting at pose error ``err``."""
    rng = random.Random(seed)
    m = G.machine(gtype)
    v = m.to_master(err)
    t, th = v[:3], v[3:]
    out = []
    for name, nom in m.fids.items():
        noise = np.array([rng.gauss(0, TRACKER_NOISE_MM) for _ in range(3)])
        out.append((name, nom + t + np.cross(th, nom - m.p0) + noise))
    return out


def write(
    path: Path, gtype: str, serial: str, err: dict[str, float], seed: int
) -> None:
    pose = ", ".join(f"{k} {v:+g}" for k, v in err.items())
    lines = [
        f"# {gtype} girder {serial} - example survey for the demo",
        f"# Girder placed at: {pose}",
        "# (mm for sway/heave/surge, mrad for roll/pitch/yaw)",
        f"# Tracker noise {TRACKER_NOISE_MM} mm 1-sigma. Master frame, mm.",
        "#",
        "# The name column is ignored: points are matched to reference fiducials",
        "# by position, because names repeat between girder types.",
        "name,x,y,z",
    ]
    for name, p in survey(gtype, err, seed):
        lines.append(f"{name},{p[0]:.4f},{p[1]:.4f},{p[2]:.4f}")
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path.name}")


if __name__ == "__main__":
    write(
        HERE / "ms_DLS0011116_initial.csv",
        "MS",
        "DLS0011116",
        {
            "roll": 0.90,
            "pitch": -0.60,
            "heave": 2.50,
            "sway": 1.20,
            "surge": -0.80,
            "yaw": 0.40,
        },
        seed=1,
    )
    write(
        HERE / "ms_DLS0011116_as_left.csv",
        "MS",
        "DLS0011116",
        {
            "roll": 0.02,
            "pitch": 0.05,
            "heave": 0.03,
            "sway": 0.04,
            "surge": 0.08,
            "yaw": 0.06,
        },
        seed=2,
    )
    write(
        HERE / "lm_DLS0011115_initial.csv",
        "LM",
        "DLS0011115",
        {
            "roll": -0.45,
            "pitch": 0.30,
            "heave": -1.10,
            "sway": -0.70,
            "surge": 0.50,
            "yaw": -0.25,
        },
        seed=3,
    )
