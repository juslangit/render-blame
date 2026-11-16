# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn a probe render (multilayer EXR) into a cost breakdown.

Pure numpy + OpenImageIO, both bundled with Blender, so this runs inside
Blender or in Blender's Python without bpy.

The probe EXR holds:
  * "Render Time"  - Cycles' per-pixel render time (CPU only)
  * CryptoObject / CryptoMaterial - which object/material covers each pixel,
    with coverage weights, so edge pixels are shared fairly.
"""
import json
import numpy as np
import OpenImageIO as oiio

BACKGROUND = "(world / background)"


def read_exr(path):
    """Return ({channel: 2D float array}, {attribute: value}) for every part."""
    img = oiio.ImageInput.open(path)
    if img is None:
        raise RuntimeError(f"Cannot open {path}: {oiio.geterror()}")
    layers, attrs, sub = {}, {}, 0
    while img.seek_subimage(sub, 0):
        spec = img.spec()
        px = img.read_image(format=oiio.FLOAT)
        for i, ch in enumerate(spec.channelnames):
            layers[ch] = px[..., i]
        for a in spec.extra_attribs:
            attrs[a.name] = a.value
        sub += 1
    img.close()
    return layers, attrs


def hash_to_float(hex_hash):
    """Cryptomatte manifest hash -> the float32 id stored in the image.

    The spec flips bit 23 when the exponent is 0 or 255 so the id is never
    a denormal, inf or NaN.
    """
    h = int(hex_hash, 16)
    exp = (h >> 23) & 255
    if exp in (0, 255):
        h ^= 1 << 23
    return np.array([h], dtype=np.uint32).view(np.float32)[0]


def manifests(attrs):
    """{'CryptoObject': {name: float_id}, 'CryptoMaterial': {...}}"""
    out = {}
    keys = {k.split("/")[1] for k in attrs if k.startswith("cryptomatte/")}
    for key in keys:
        name = attrs.get(f"cryptomatte/{key}/name", "")
        manifest = attrs.get(f"cryptomatte/{key}/manifest")
        if not manifest:
            continue
        kind = name.split(".")[-1]  # ViewLayer.CryptoObject -> CryptoObject
        out[kind] = {n: hash_to_float(h) for n, h in json.loads(manifest).items()}
    return out


def find(layers, suffix):
    for ch in layers:
        if ch.endswith(suffix):
            return layers[ch]
    return None


def _ranks(layers, kind):
    """[(ids, coverage)] for every Cryptomatte rank of this kind."""
    out = []
    for base in sorted({ch.rsplit(".", 1)[0] for ch in layers if f".{kind}" in ch}):
        for id_ch, cov_ch in (("r", "g"), ("b", "a")):  # two ranks per RGBA layer
            ids, cov = layers.get(f"{base}.{id_ch}"), layers.get(f"{base}.{cov_ch}")
            if ids is not None and cov is not None:
                out.append((ids.astype(np.float32), cov))
    return out


def attribute(layers, cost, kind, manifest, see_through=()):
    """Share every pixel's cost among the ids that cover it.

    `see_through` names glass / transmissive / alpha-cut objects or materials.
    Cryptomatte records them *and* whatever is seen through them on the same
    pixel; hiding the thing behind the glass saves nothing (the pixel still
    goes through glass), so on such pixels the see-through ids own the whole
    cost and the others are only counted as seen through.

    Returns {name: (cost, coverage_pixels, seen_through_pixels)};
    uncovered cost goes to BACKGROUND.
    """
    ranks = _ranks(layers, kind)
    owner_ids = np.array([manifest[n] for n in see_through if n in manifest], dtype=np.float32)
    owner_cov = np.zeros_like(cost)
    if owner_ids.size:
        for ids, cov in ranks:
            owner_cov += np.where(np.isin(ids, owner_ids), cov, 0.0)
    glassy = owner_cov > 0
    by_id = {}
    covered = np.zeros_like(cost)
    for ids, cov in ranks:
        mask = cov > 0
        for u in np.unique(ids[mask]):
            m = mask & (ids == u)
            if u in owner_ids:
                w = np.where(glassy[m], cov[m] / np.maximum(owner_cov[m], 1e-9), cov[m])
                through = 0.0
            else:
                w = np.where(glassy[m], 0.0, cov[m])
                through = float(cov[m][glassy[m]].sum())
            c, p, t = by_id.get(float(u), (0.0, 0.0, 0.0))
            by_id[float(u)] = (c + float((cost[m] * w).sum()), p + float(w.sum()), t + through)
            covered[m] += w
    names = {float(v): n for n, v in manifest.items()}
    result = {}
    for u, (c, p, t) in by_id.items():
        name = names.get(u, f"(unknown {u:.3g})")
        oc, op, ot = result.get(name, (0.0, 0.0, 0.0))
        result[name] = (oc + c, op + p, ot + t)
    rest = np.clip(1.0 - covered, 0.0, 1.0)
    rest[glassy] = 0.0  # the background seen through glass belongs to the glass too
    result[BACKGROUND] = (float((cost * rest).sum()), float(rest.sum()), 0.0)
    return result


def total_cost(path):
    """Sum of the Render Time pass of a probe EXR."""
    layers, _ = read_exr(path)
    cost = find(layers, "Render Time.X")
    if cost is None:
        raise RuntimeError("No Render Time pass in the probe render (it is CPU-only).")
    return float(cost.sum())


def breakdown(path, see_through=None):
    """Full report dict from a probe EXR.

    see_through: {"CryptoObject": names, "CryptoMaterial": names} - see attribute().
    """
    see_through = see_through or {}
    layers, attrs = read_exr(path)
    cost = find(layers, "Render Time.X")
    if cost is None:
        raise RuntimeError("No Render Time pass in the probe render (it is CPU-only).")
    total = float(cost.sum()) or 1.0
    npx = cost.size
    avg = total / npx
    man = manifests(attrs)
    report = {"pixels": npx, "width": cost.shape[1], "height": cost.shape[0],
              "total_cost": total}
    for kind, key in (("CryptoObject", "objects"), ("CryptoMaterial", "materials")):
        if kind not in man:
            report[key] = []
            continue
        rows = []
        for name, (c, p, t) in attribute(layers, cost, kind, man[kind],
                                         see_through.get(kind, ())).items():
            if p < 0.5 and t < 0.5 and c / total < 0.001:
                continue
            rows.append({
                "name": name,
                "share": c / total,                     # of total render time
                "screen": p / npx,                      # of the frame it covers
                "density": (c / p / avg) if p else 0.0,  # x the average pixel
                "through": t / npx,                     # of the frame, seen through glass
            })
        rows.sort(key=lambda r: r["share"], reverse=True)
        report[key] = rows
    return report, layers


def _box_blur(a, r):
    """Separable box blur with edge padding, two passes (close to a Gaussian)."""
    for _ in range(2):
        for axis in (0, 1):
            p = np.pad(a, [(r + 1, r) if ax == axis else (0, 0) for ax in (0, 1)], mode="edge")
            c = np.cumsum(p, axis=axis)
            hi = np.take(c, range(2 * r + 1, c.shape[axis]), axis=axis)
            lo = np.take(c, range(0, c.shape[axis] - 2 * r - 1), axis=axis)
            a = (hi - lo) / (2 * r + 1)
    return a


def heatmap(layers, out_png):
    """Cost heatmap over a dimmed greyscale of the render, saved as PNG."""
    cost = find(layers, "Render Time.X")
    r, g, b = (find(layers, f"Combined.{c}") for c in "RGB")
    grey = np.clip(0.2126 * r + 0.7152 * g + 0.0722 * b, 0, 1) ** (1 / 2.2)
    # log scale, 2nd..99th percentile, so one hot pixel doesn't flatten the rest
    # per-pixel timing is noisy (measured per tile row), so smooth the picture;
    # the numbers in the report stay unsmoothed
    cost = _box_blur(cost, max(2, cost.shape[1] // 120))
    lc = np.log1p(cost / (np.median(cost) or 1.0))
    lo, hi = np.percentile(lc, 2), np.percentile(lc, 99)
    t = np.clip((lc - lo) / ((hi - lo) or 1.0), 0, 1)
    # dark blue -> purple -> orange -> yellow
    stops = np.array([[0.05, 0.05, 0.25], [0.45, 0.10, 0.55],
                      [0.95, 0.45, 0.10], [1.00, 0.95, 0.40]])
    x = t * (len(stops) - 1)
    i = np.minimum(x.astype(int), len(stops) - 2)
    f = (x - i)[..., None]
    col = stops[i] * (1 - f) + stops[i + 1] * f
    out = 0.35 * grey[..., None] + 0.65 * col
    rgba = np.dstack([out, np.ones_like(grey)]).astype(np.float32)
    h, w = grey.shape
    spec = oiio.ImageSpec(w, h, 4, oiio.UINT8)
    o = oiio.ImageOutput.create(out_png)
    o.open(out_png, spec)
    o.write_image(rgba)
    o.close()
    return out_png
