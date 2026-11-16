# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs inside a background Blender on a *copy* of the user's file.

  blender -b copy.blend --python probe.py -- <out_dir> <settings.json>

Writes <out_dir>/report.json and <out_dir>/heatmap.png. The user's own file
and scene are never touched, so nothing has to be restored afterwards.
"""
import json
import os
import re
import sys
import time

import bpy

# this runs as a plain script in a separate Blender, so load the sibling module directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402

MAX_VOLUME_CHECKS = 6


PROGRESS = None  # file the add-on reads to show what is happening


def log(msg):
    print(f"RENDER_BLAME: {msg}", flush=True)
    if PROGRESS:
        try:
            with open(PROGRESS, "w") as f:
                f.write(msg)
        except OSError:
            pass


def setup(scene, settings, exr_path):
    scene.render.engine = "CYCLES"
    # Cycles only writes the Render Time pass on the CPU, so the probe always
    # runs there, whatever device the user renders with. Relative cost per
    # object is what we want, and that carries over.
    scene.cycles.device = "CPU"
    scene.cycles.use_denoising = False
    if settings["max_samples"] > 0:
        scene.cycles.samples = min(scene.cycles.samples, settings["max_samples"])
    scene.render.resolution_percentage = max(
        1, int(scene.render.resolution_percentage * settings["resolution"] / 100))
    scene.render.use_border = False
    scene.render.use_compositing = False
    scene.render.use_sequencer = False
    active = bpy.context.view_layer
    for vl in scene.view_layers:
        vl.use = vl == active
    active.cycles.pass_render_time = True
    active.use_pass_cryptomatte_object = True
    active.use_pass_cryptomatte_material = True
    active.pass_cryptomatte_depth = 6
    s = scene.render.image_settings
    s.media_type = "MULTI_LAYER_IMAGE"
    s.color_depth = "32"   # crypto ids must stay full float
    scene.render.filepath = exr_path


def render(scene, path):
    scene.render.filepath = path
    t = time.time()
    bpy.ops.render.render(write_still=True)
    return time.time() - t


def has_volume(mat):
    if not mat or not mat.node_tree:
        return False
    for n in mat.node_tree.nodes:
        if n.type == "OUTPUT_MATERIAL" and n.inputs["Volume"].is_linked:
            return True
    return False


def material_hint(mat):
    """A short, plain reason a material may be costly. Empty if nothing stands out."""
    if not mat or not mat.node_tree:
        return ""
    hints = []
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            def val(name):
                i = n.inputs.get(name)
                return (i.is_linked or (i.default_value > 0)) if i else False
            if val("Transmission Weight"):
                hints.append("glass")
            if val("Subsurface Weight"):
                hints.append("subsurface")
            if n.inputs.get("Coat Weight") and val("Coat Weight"):
                hints.append("coat")
            alpha = n.inputs.get("Alpha")
            if alpha and (alpha.is_linked or alpha.default_value < 1.0):
                hints.append("alpha cut-out")
            es, ec = n.inputs.get("Emission Strength"), n.inputs.get("Emission Color")
            if es and ec and (es.is_linked or es.default_value > 0) and (
                    ec.is_linked or max(ec.default_value[:3]) > 0):
                hints.append("glows")
            met, rough = n.inputs.get("Metallic"), n.inputs.get("Roughness")
            if (met and rough and not met.is_linked and not rough.is_linked
                    and met.default_value >= 0.9 and rough.default_value < 0.1):
                hints.append("mirror metal")
        elif n.type in {"BSDF_GLASS", "BSDF_REFRACTION"}:
            hints.append("glass")
        elif n.type == "SUBSURFACE_SCATTERING":
            hints.append("subsurface")
        elif n.type in {"BSDF_HAIR", "BSDF_HAIR_PRINCIPLED"}:
            hints.append("hair")
        elif n.type == "BSDF_TRANSPARENT":
            hints.append("transparency")
        elif n.type == "EMISSION":
            hints.append("glows")
    if has_volume(mat):
        hints.append("volume")
    return ", ".join(dict.fromkeys(hints))


SEE_THROUGH = ("glass", "transparency", "alpha")


def is_see_through(mat):
    h = material_hint(mat)
    return any(k in h for k in SEE_THROUGH)


# Materials whose cost lands on *other* pixels: light from glowing meshes is
# sampled by every surface, shadows pass through glass and alpha, mirrors
# trace the rest of the scene. Pixel attribution misses that, so these are
# measured directly: swap to a plain material, render, see what it saves.
INDIRECT = SEE_THROUGH + ("glows", "mirror")
MAX_SWAP_CHECKS = 4


def is_indirect(mat):
    h = material_hint(mat)
    return any(k in h for k in INDIRECT)


def parts_of(root):
    out, todo = [], [root]
    while todo:
        o = todo.pop()
        out.append(o)
        todo += list(o.children)
    return out


def model_of(ob):
    """The top-level parent: the imported model an 'Object_2' belongs to."""
    while ob.parent is not None:
        ob = ob.parent
    return ob.name


GENERIC = re.compile(r"^(sketchfab_model|root|rootnode|gltf_scenerootnode|scene|empty|armature|"
                     r"[0-9a-f]{32}|object_\d+)$", re.I)


def model_label(root):
    """A readable name for an imported model. Importers wrap models in nodes
    called 'Sketchfab_model', 'root' or a 32-character hash; skip those."""
    chain, ob = [root], root
    while len(ob.children) == 1:
        ob = ob.children[0]
        chain.append(ob)
    for o in chain:
        name = re.sub(r"\.\d{3}$", "", o.name)
        name = re.sub(r"\.(fbx|obj|gltf|glb|blend|dae)\b.*$", "", name, flags=re.I)
        if name and not GENERIC.match(name):
            return name
    kids = list(ob.children)
    if not kids:
        return root.name
    first = re.sub(r"\.\d{3}$", "", kids[0].name)
    return f"{first} +{len(kids) - 1}" if len(kids) > 1 else first


def add_models(report, scene):
    """Rows per top-level model, summed from the object rows."""
    models = {}
    for row in report["objects"]:
        ob = scene.objects.get(row["name"])
        key = model_of(ob) if ob else row["name"]
        label = model_label(scene.objects[key]) if key in scene.objects else key
        m = models.setdefault(key, {"name": key, "label": label, "share": 0.0, "screen": 0.0,
                                    "through": 0.0, "parts": 0, "hints": []})
        m["share"] += row["share"]
        m["screen"] += row["screen"]
        m["through"] += row.get("through", 0.0)
        m["parts"] += 1
        if row.get("hint"):
            m["hints"] += row["hint"].split(", ")
    rows = []
    for m in models.values():
        m["density"] = m["share"] / m["screen"] if m["screen"] else 0.0  # = cost per pixel vs average
        m["hint"] = ", ".join(dict.fromkeys(m.pop("hints")))
        rows.append(m)
    rows.sort(key=lambda r: r["share"], reverse=True)
    report["models"] = rows


def volume_suspects(scene):
    out = []
    layer_objects = bpy.context.view_layer.objects
    for ob in scene.objects:
        # Only render visibility counts: an object hidden in the viewport
        # while you work still renders, so it must still be measured.
        if (ob.hide_render or ob.name not in layer_objects
                or any(c.hide_render for c in ob.users_collection)):
            continue
        if ob.type == "VOLUME" or any(has_volume(s.material) for s in getattr(ob, "material_slots", [])):
            out.append(ob)
    return out[:MAX_VOLUME_CHECKS]


def swap_suspects(scene, report):
    """Models (not volumes, not the world) with a material whose cost lands
    elsewhere, biggest share first."""
    out = []
    for row in report.get("models", []):
        ob = scene.objects.get(row["name"])
        if ob is None:
            continue
        mats = [sl.material for o in parts_of(ob) for sl in getattr(o, "material_slots", []) if sl.material]
        if any(is_indirect(m) for m in mats) and not any(has_volume(m) for m in mats):
            out.append(row)
    return out[:MAX_SWAP_CHECKS]


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    out_dir, settings_path = argv[0], argv[1]
    global PROGRESS
    PROGRESS = os.path.join(out_dir, "progress.txt")
    with open(settings_path) as f:
        settings = json.load(f)
    scene = bpy.context.scene
    base = os.path.join(out_dir, "probe.exr")
    setup(scene, settings, base)

    log("test render")
    wall = render(scene, base)
    see_through = {
        "CryptoObject": [ob.name for ob in scene.objects
                         if any(is_see_through(sl.material) for sl in getattr(ob, "material_slots", []) if sl.material)],
        "CryptoMaterial": [m.name for m in bpy.data.materials if is_see_through(m)],
    }
    report, layers = analyze.breakdown(base, see_through)
    report["wall_seconds"] = wall
    report["heatmap"] = analyze.heatmap(layers, os.path.join(out_dir, "heatmap.png"))

    for row in report["materials"]:
        row["hint"] = material_hint(bpy.data.materials.get(row["name"]))
    obj_hint = {}
    for ob in scene.objects:
        mats = [s.material for s in getattr(ob, "material_slots", []) if s.material]
        obj_hint[ob.name] = ", ".join(dict.fromkeys(h for m in mats for h in material_hint(m).split(", ") if h))
    for row in report["objects"]:
        row["hint"] = obj_hint.get(row["name"], "")
        ob = scene.objects.get(row["name"])
        row["model"] = model_label(scene.objects[model_of(ob)]) if ob and ob.parent else ""
    add_models(report, scene)

    # Volumes write no object id and no cryptomatte, so their cost lands on
    # whatever is behind them. Measure them the slow, honest way: hide each
    # one, render again, and see how much time disappears.
    # Render time is noisy from run to run, so each hidden cost is measured
    # REPEATS times against a fresh full render each time, and reported as
    # the middle value with its range.
    report["volumes"] = []
    check = settings.get("check_volumes", True)
    suspects = volume_suspects(scene) if check else []
    swaps = swap_suspects(scene, report) if check else []
    repeats = max(1, int(settings.get("volume_repeats", 2)))
    bases = [report["total_cost"]]
    if suspects or swaps:
        for r in range(1, repeats):
            p = os.path.join(out_dir, f"base_{r}.exr")
            render(scene, p)
            bases.append(analyze.total_cost(p))
            os.remove(p)
    total_checks = len(suspects) + len(swaps)

    def measure(i, label, change, undo):
        saved = []
        for r in range(repeats):
            log(f"check {i} of {total_checks}: {label} (pass {r + 1})")
            change()
            p = os.path.join(out_dir, f"check_{i}_{r}.exr")
            render(scene, p)
            undo()
            saved.append((bases[r] - analyze.total_cost(p)) / bases[r])
            os.remove(p)
        saved.sort()
        return saved[len(saved) // 2], saved[0], saved[-1]

    for i, ob in enumerate(suspects, 1):
        mid, lo, hi = measure(i, ob.name, lambda: setattr(ob, "hide_render", True),
                              lambda: setattr(ob, "hide_render", False))
        report["volumes"].append({"name": ob.name, "share": max(0.0, mid),
                                  "low": max(0.0, lo), "high": max(0.0, hi), "hint": "volume"})

    plain = bpy.data.materials.new("RenderBlame_plain")
    for i, row in enumerate(swaps, len(suspects) + 1):
        slots = [sl for o in parts_of(scene.objects[row["name"]]) for sl in getattr(o, "material_slots", [])]
        kept = [(sl.link, sl.material) for sl in slots]

        def change():
            for sl in slots:
                sl.link = "OBJECT"
                sl.material = plain

        def undo():
            for sl, (link, mat) in zip(slots, kept):
                sl.link = link
                if link == "OBJECT":
                    sl.material = mat
        mid, lo, hi = measure(i, row.get("label", row["name"]), change, undo)
        row["fix"], row["fix_low"], row["fix_high"] = mid, lo, hi
        for orow in report["objects"]:  # an un-parented object is its own model
            if orow["name"] == row["name"]:
                orow["fix"], orow["fix_low"], orow["fix_high"] = mid, lo, hi

    with open(os.path.join(out_dir, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    log("done")


try:
    main()
except Exception as e:  # report the reason to the add-on instead of a silent exit
    import traceback
    traceback.print_exc()
    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(os.path.join(argv[0], "report.json"), "w") as f:
        json.dump({"error": str(e)}, f)
    sys.exit(1)
