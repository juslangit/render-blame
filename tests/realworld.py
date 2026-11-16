"""Real-world check on downloaded Sketchfab models (tests/sketchfab/, not in git).

Step 1 - build: blender -b --factory-startup --python tests/realworld.py -- build <out.blend> <a.glb> [b.glb ...] [--fog]
Step 2 - truth: blender -b <scene.blend> --factory-startup --python tests/realworld.py -- truth <report.json> <out.json>

`truth` hides each of the top objects in turn, renders again, and records how
much render time really disappeared - the answer Render Blame's ranking should
agree with.
"""
import json
import math
import os
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "render_blame"))
import analyze  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:]
mode = argv[0]


def bounds(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs if o.type == "MESH" for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def import_fit(path, x, size=2.0):
    """Import a glb, scale its largest side to `size` m, stand it on the floor at x."""
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    roots = [o for o in new if o.parent is None]
    bpy.context.view_layer.update()
    lo, hi = bounds(new)
    s = size / max(hi - lo)
    for r in roots:
        r.scale *= s
    bpy.context.view_layer.update()
    lo, hi = bounds(new)
    centre = (lo + hi) / 2
    for r in roots:
        r.location += Vector((x - centre.x, -centre.y, -lo.z))
    return new


def build(out, glbs, fog):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = 128
    sc.render.resolution_x, sc.render.resolution_y = 1280, 720
    spacing = 2.6
    cols = 2 if len(glbs) > 1 else 1
    slots = []
    for i in range(len(glbs)):
        c, r = i % cols, i // cols
        slots.append(((c - (cols - 1) / 2) * spacing, r * spacing))
    for path, (x, y) in zip(glbs, slots):
        new = import_fit(path, x)
        for o in new:
            if o.parent is None:
                o.location.y += y
    bpy.ops.mesh.primitive_plane_add(size=60)
    floor = bpy.context.object
    floor.name = "Floor"
    m = bpy.data.materials.new("M_Floor")
    floor.data.materials.append(m)
    if fog:
        bpy.ops.mesh.primitive_cube_add(location=(0, 0, 1.2))
        f = bpy.context.object
        f.name = "Fog"
        f.location = (0, (len(slots) > 2) * spacing / 2, 1.2)
        f.scale = (cols * spacing / 2 + 0.4, (len(slots) > 2) * spacing / 2 + 1.4, 1.2)
        fm = bpy.data.materials.new("M_Fog")
        nt = fm.node_tree
        nt.nodes.remove(next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"))
        v = nt.nodes.new("ShaderNodeVolumePrincipled")
        v.inputs["Density"].default_value = 0.15
        nt.links.new(v.outputs[0], next(n for n in nt.nodes if n.type == "OUTPUT_MATERIAL").inputs["Volume"])
        f.data.materials.append(fm)
    bpy.ops.object.light_add(type="AREA", location=(2, -4, 6))
    bpy.context.object.data.energy = 1500
    bpy.context.object.data.size = 3
    rows = (len(glbs) + cols - 1) // cols
    target_y = (rows - 1) * spacing / 2
    dist = 3.2 + 2.4 * max(cols, rows)
    bpy.ops.object.empty_add(location=(0, target_y, 0.9))
    aim = bpy.context.object
    bpy.ops.object.camera_add(location=(0, target_y - dist, 0.9 + dist * 0.45))
    cam = bpy.context.object
    t = cam.constraints.new("TRACK_TO")
    t.target = aim
    t.track_axis, t.up_axis = "TRACK_NEGATIVE_Z", "UP_Y"
    sc.camera = cam
    sc.world = bpy.data.worlds.new("W")
    sc.world.color = (0.05, 0.05, 0.06)
    bpy.ops.wm.save_as_mainfile(filepath=out)
    print("BUILT", out, len(bpy.data.objects), "objects")


def truth(report_path, out, top=4):
    """Hide each top object (by share), render, measure the time that disappears."""
    with open(report_path) as f:
        report = json.load(f)
    sc = bpy.context.scene
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "render_blame"))
    tmp = os.path.dirname(out)
    # same settings as the probe
    import importlib.util
    spec = importlib.util.spec_from_file_location("probe_mod", os.path.join(os.path.dirname(HERE), "render_blame", "probe.py"))
    src = open(spec.origin).read().split("\ntry:\n    main()")[0]  # functions only, not the run
    ns = {"__name__": "probe_mod", "__file__": spec.origin}
    exec(compile(src, spec.origin, "exec"), ns)
    ns["setup"](sc, {"resolution": 25, "max_samples": 64}, os.path.join(tmp, "t.exr"))
    rows = [r for r in report["objects"] if r["name"] in bpy.data.objects][:top]
    results = []
    base = []
    for _ in range(2):
        ns["render"](sc, os.path.join(tmp, "t.exr"))
        base.append(analyze.total_cost(os.path.join(tmp, "t.exr")))
    b = sum(base) / len(base)
    for r in rows:
        ob = bpy.data.objects[r["name"]]
        ob.hide_render = True
        ns["render"](sc, os.path.join(tmp, "t.exr"))
        ob.hide_render = False
        saved = (b - analyze.total_cost(os.path.join(tmp, "t.exr"))) / b
        results.append({"name": r["name"], "predicted": r["share"], "density": r["density"],
                        "screen": r["screen"], "saved_when_hidden": saved})
        print(f"TRUTH {r['name']:30s} predicted {r['share']*100:5.1f}%  saved {saved*100:5.1f}%  density {r['density']:.2f}")
    # whole models: hide every part at once
    def parts(root):
        out, todo = [], [root]
        while todo:
            o = todo.pop()
            out.append(o)
            todo += list(o.children)
        return out
    for m in [m for m in report.get("models", []) if m["name"] in bpy.data.objects][:top]:
        obs = parts(bpy.data.objects[m["name"]])
        for o in obs:
            o.hide_render = True
        ns["render"](sc, os.path.join(tmp, "t.exr"))
        for o in obs:
            o.hide_render = False
        saved = (b - analyze.total_cost(os.path.join(tmp, "t.exr"))) / b
        results.append({"name": "MODEL " + m.get("label", m["name"]), "predicted": m["share"],
                        "density": m["density"], "saved_when_hidden": saved})
        print(f"TRUTH MODEL {m.get('label', m['name'])[:24]:24s} predicted {m['share']*100:5.1f}%  saved {saved*100:5.1f}%  density {m['density']:.2f}")
    with open(out, "w") as f:
        json.dump({"base_noise": (max(base) - min(base)) / b, "rows": results}, f, indent=1)


def swap(report_path, out, top=4):
    """The fix test: give a model's parts a plain grey material and measure the
    speed-up. Render Blame predicts about share x (1 - 1/density)."""
    with open(report_path) as f:
        report = json.load(f)
    sc = bpy.context.scene
    tmp = os.path.dirname(out)
    src = open(os.path.join(os.path.dirname(HERE), "render_blame", "probe.py")).read().split("\ntry:\n    main()")[0]
    ns = {"__name__": "probe_mod", "__file__": os.path.join(os.path.dirname(HERE), "render_blame", "probe.py")}
    exec(compile(src, ns["__file__"], "exec"), ns)
    ns["setup"](sc, {"resolution": 25, "max_samples": 64}, os.path.join(tmp, "t.exr"))
    grey = bpy.data.materials.new("RB_plain_grey")
    base = []
    for _ in range(2):
        ns["render"](sc, os.path.join(tmp, "t.exr"))
        base.append(analyze.total_cost(os.path.join(tmp, "t.exr")))
    b = sum(base) / len(base)
    rows = []
    for m in [m for m in report["models"] if m["name"] in bpy.data.objects and m["name"] != "Floor"][:top]:
        todo, obs = [bpy.data.objects[m["name"]]], []
        while todo:
            o = todo.pop(); obs.append(o); todo += list(o.children)
        saved_slots = []
        for o in obs:
            for sl in getattr(o, "material_slots", []):
                saved_slots.append((sl, sl.link, sl.material))
                sl.link = "OBJECT"
                sl.material = grey
        ns["render"](sc, os.path.join(tmp, "t.exr"))
        for sl, link, mat in saved_slots:
            sl.link = link
            if link == "OBJECT":
                sl.material = mat
        saved = (b - analyze.total_cost(os.path.join(tmp, "t.exr"))) / b
        pred = m["share"] * (1 - 1 / m["density"]) if m["density"] > 0 else 0.0
        rows.append({"model": m.get("label"), "density": m["density"], "predicted_saving": pred, "saved": saved})
        print(f"SWAP {m.get('label', m['name'])[:28]:28s} density {m['density']:.2f}  predicted {pred*100:5.1f}%  real {saved*100:5.1f}%")
    with open(out, "w") as f:
        json.dump({"base_noise": (max(base) - min(base)) / b, "rows": rows}, f, indent=1)


if mode == "build":
    build(argv[1], [a for a in argv[2:] if a.endswith(".glb")], "--fog" in argv)
elif mode == "truth":
    truth(argv[1], argv[2])
elif mode == "swap":
    swap(argv[1], argv[2])
