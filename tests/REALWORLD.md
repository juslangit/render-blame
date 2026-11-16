# Real-world check — 2026-11-16

Five free Sketchfab models (downloaded to `tests/sketchfab/`, not in git) built into
test scenes by `tests/realworld.py`, then Render Blame's answer checked against
reality two ways:

- **hide** — hide the model, render again, measure the time that disappears;
- **plain** — give the model a plain grey material, render again, measure the speed-up
  (the fix you would actually try).

Probe: 25% size, 64 samples, CPU, Blender 5.2.1, MacBook Air M2. Run-to-run noise ≈ 0.6%.

## Mixed scene — ship in a bottle, crystal chandelier, monstera, scanned statue, fog

| Model | Render Blame: share | Hide test | Per pixel | Plain test | Render Blame "plain saves" |
|---|---|---|---|---|---|
| Ship in a bottle | 25.0% | 24.1% | 3.35× | 21.8% | 19% |
| Floor | 26.6% | 23.9% | 0.73× | — | — |
| Crystal chandelier | 5.4% | 7.9% | 1.28× | 19.5% | 18–19% |
| Monstera | 3.7% | 2.5% | 0.93× | 0.3% | (none) |
| Statue (95k scan) | 3.6% | 4.0% | 1.05× | −0.9% | (none) |
| Fog volume | 48–50% (hidden test) | | | | |

Single bottle: 42.9% predicted vs 46.8% hide test. Living-room interior (47 objects) ran
cleanly; walls show the limit below.

## What this run changed (v0.1.1)

1. **Glass owns what is seen through it.** Cryptomatte records the glass *and* the ship
   inside it on the same pixels; the ship was blamed for 3.7% but hiding it saved
   nothing. Now see-through objects take those pixels' time and the others are flagged
   "seen through glass".
2. **Hidden costs.** The chandelier's glowing bulbs, mirror metal and alpha glass cost
   19.5% — almost none of it on its own pixels (it lights and shadows the rest). Models
   with such materials are now measured directly with the plain-material swap.
3. **Models view** (default): Sketchfab imports arrive as dozens of `Object_N` parts;
   rows now sum per top-level model, with readable names past `Sketchfab_model`,
   `root` and hash wrappers.
4. **Viewport-hidden volumes** are now measured (they still render).

## The limit worth knowing
"Share of time" is the time spent on an object's pixels — not what deleting it saves.
Hide a wall and the wall behind shows through at the same cost (interior: a wall at
14.6% saved −1.1% when hidden). For expensive materials, the plain-material number is
the one that predicts the fix.
