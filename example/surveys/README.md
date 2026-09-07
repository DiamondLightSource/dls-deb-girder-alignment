# Example surveys

Laser-tracker surveys for trying the tool in `--demo` mode. Each file places a
girder at a **known** pose error, so the pose the tool fits back out can be
checked against the header comment.

| file | girder | serial | pose |
|---|---|---|---|
| `ms_DLS0011116_initial.csv` | MS | DLS0011116 | as-delivered: 2.5 mm heave, 0.9 mrad roll |
| `ms_DLS0011116_as_left.csv` | MS | DLS0011116 | in tolerance on every axis |
| `lm_DLS0011115_initial.csv` | LM | DLS0011115 | a different girder type |

All carry 0.015 mm 1-sigma noise, which is tracker-scale, so the fit comes back
with an RMS around 0.02 mm rather than a suspiciously perfect zero.

## Trying it

```bash
dls-deb-girder-alignment serve --demo --config example/config/config.yaml
```

1. Serial `DLS0011116`, any operator name, **Combined vertical** on.
2. Load `ms_DLS0011116_initial.csv`. The fit reports ~0.02 mm RMS and every axis
   out of tolerance, and a four-step plan appears.
3. For each step press **Simulate move**, wait for the gate to go green, then
   **Confirm step complete**. (*Simulate move* drives the full rigid state, so
   the un-moved jack pair shifts by the cross-shift as real steel would.)
4. After the last step, load `ms_DLS0011116_as_left.csv`. Every axis is now in
   tolerance and the session completes.
5. **Generate report** for the PDF and its JSON sibling.

## Making more

`make_surveys.py` generates these. Edit the pose dictionaries at the bottom and
re-run it to build training cases — a girder needing only a roll correction, one
far enough out to be worth two iterations, and so on:

```bash
python example/surveys/make_surveys.py
```
