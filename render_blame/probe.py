# SPDX-License-Identifier: GPL-3.0-or-later
"""Runs inside a background Blender on a *copy* of the user's file.

  blender -b copy.blend --python probe.py -- <out_dir> <settings.json>

Writes <out_dir>/report.json and <out_dir>/heatmap.png. The user's own file
and scene are never touched, so nothing has to be restored afterwards.
"""
import json
import os
import sys
import time

import bpy

# this runs as a plain script in a separate Blender, so load the sibling module directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402

MAX_VOLUME_CHECKS = 6


def log(msg):
    print(f"RENDER_BLAME: {msg}", flush=True)


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
                hints.append("glass / transmission")
            if val("Subsurface Weight"):
                hints.append("subsurface")
            if n.inputs.get("Coat Weight") and val("Coat Weight"):
                hints.append("coat layer")
        elif n.type in {"BSDF_GLASS", "BSDF_REFRACTION"}:
            hints.append("glass / refraction")
        elif n.type == "SUBSURFACE_SCATTERING":
            hints.append("subsurface")
        elif n.type in {"BSDF_HAIR", "BSDF_HAIR_PRINCIPLED"}:
            hints.append("hair shading")
        elif n.type == "BSDF_TRANSPARENT":
            hints.append("transparency (extra bounces)")
    if has_volume(mat):
        hints.append("volume")
    return ", ".join(dict.fromkeys(hints))


def volume_suspects(scene):
    out = []
    for ob in scene.objects:
        if ob.hide_render or not ob.visible_get():
            continue
        if ob.type == "VOLUME" or any(has_volume(s.material) for s in getattr(ob, "material_slots", [])):
            out.append(ob)
    return out[:MAX_VOLUME_CHECKS]


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    out_dir, settings_path = argv[0], argv[1]
    with open(settings_path) as f:
        settings = json.load(f)
    scene = bpy.context.scene
    base = os.path.join(out_dir, "probe.exr")
    setup(scene, settings, base)

    log("rendering probe")
    wall = render(scene, base)
    report, layers = analyze.breakdown(base)
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

    # Volumes write no object id and no cryptomatte, so their cost lands on
    # whatever is behind them. Measure them the slow, honest way: hide each
    # one, render again, and see how much time disappears.
    report["volumes"] = []
    if settings.get("check_volumes", True):
        for i, ob in enumerate(volume_suspects(scene)):
            log(f"checking volume {i + 1}: {ob.name}")
            ob.hide_render = True
            p = os.path.join(out_dir, f"without_{i}.exr")
            render(scene, p)
            ob.hide_render = False
            without, _ = analyze.breakdown(p)
            saved = (report["total_cost"] - without["total_cost"]) / report["total_cost"]
            report["volumes"].append({"name": ob.name, "share": max(0.0, saved),
                                      "hint": "volume"})
            os.remove(p)

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
