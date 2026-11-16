# SPDX-License-Identifier: GPL-3.0-or-later
"""Render Blame - find which objects and materials make a Cycles render slow.

Properties > Render > Render Blame > Find What's Slow.
A background Blender renders a small copy of the scene on the CPU with
Cycles' Render Time pass and Cryptomatte, then splits the time by object and
material. Volumes, which those passes cannot see, are measured by hiding
each one and rendering again.
"""
import json
import os
import shutil
import subprocess
import tempfile

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, IntProperty, PointerProperty,
                       StringProperty)

HEATMAP_NAME = "Render Blame Heatmap"
HOT = 1.5        # cost per pixel at or above this many times average = hot
BACKGROUND = "(world / background)"

_job = {"proc": None, "dir": None, "scene": None}


# ---------------------------------------------------------------- data

class RB_Row(bpy.types.PropertyGroup):
    name: StringProperty()
    kind: StringProperty()           # OBJECT / MATERIAL / MODEL / VOLUME
    share: FloatProperty()           # of total render time, 0..1
    screen: FloatProperty()          # of the frame, 0..1
    density: FloatProperty()         # cost per pixel vs average
    low: FloatProperty()             # volumes: lowest and highest of repeated measurements
    high: FloatProperty()
    hint: StringProperty()
    model: StringProperty()          # objects: readable name of the model it belongs to
    label: StringProperty()          # models: readable name (row name stays the real object)
    through: FloatProperty()         # share of the frame where it is seen through glass
    fix: FloatProperty(default=-1.0)  # time saved with a plain material (-1 = not measured)
    fix_low: FloatProperty()
    fix_high: FloatProperty()


class RB_Settings(bpy.types.PropertyGroup):
    group_by: EnumProperty(
        name="Group by",
        items=[("OBJECT", "Objects", "Cost per object"),
               ("MATERIAL", "Materials", "Cost per material"),
               ("MODEL", "Models", "Cost per model: each top-level parent with all its parts")],
        default="MODEL")
    sort_by: EnumProperty(
        name="Sort by",
        items=[("SHARE", "Share of time", "Biggest share of the whole render first"),
               ("DENSITY", "Cost per pixel", "Most expensive per pixel first, whatever its size on screen")],
        default="SHARE")
    resolution: IntProperty(
        name="Probe size", subtype="PERCENTAGE", min=5, max=100, default=25,
        description="Size of the test render, as a percentage of the final render size")
    max_samples: IntProperty(
        name="Max samples", min=0, default=64,
        description="Cap the test render's samples (0 = use the scene's)")
    check_volumes: BoolProperty(
        name="Hidden costs", default=True,
        description=("Some costs land on other pixels: volumes, glowing meshes that light the scene, "
                     "glass and alpha that shadows pass through, mirrors. Measure them directly: hide each "
                     "volume, and give each such model a plain material, then render again (two extra "
                     "test renders per check, up to 10 checks)"))
    rows: CollectionProperty(type=RB_Row)
    index: IntProperty()
    status: StringProperty()
    wall_seconds: FloatProperty()
    probe_size: StringProperty()


# ---------------------------------------------------------------- running the probe

def _probe_script():
    return os.path.join(os.path.dirname(__file__), "probe.py")


def _poll_job():
    proc = _job["proc"]
    if proc is None:
        return None
    if proc.poll() is None:
        try:
            with open(os.path.join(_job["dir"], "progress.txt")) as f:
                _job["progress"] = f.read().strip()
        except OSError:
            pass
        _redraw()
        return 0.5
    scene = bpy.data.scenes.get(_job["scene"])
    out_dir = _job["dir"]
    _job["proc"] = None
    if scene is None:
        return None
    s = scene.render_blame
    try:
        with open(os.path.join(out_dir, "report.json")) as f:
            report = json.load(f)
    except (OSError, ValueError):
        s.status = "The test render failed - see the system console"
        _redraw()
        return None
    if "error" in report:
        s.status = f"Failed: {report['error']}"
        _redraw()
        return None
    _load_report(s, report)
    _load_heatmap(report.get("heatmap"))
    s.status = "done"
    _redraw()
    return None


def _load_report(s, report):
    s.rows.clear()
    for kind, key in (("OBJECT", "objects"), ("MATERIAL", "materials"),
                      ("MODEL", "models"), ("VOLUME", "volumes")):
        for r in report.get(key, []):
            row = s.rows.add()
            row.name, row.kind = r["name"], kind
            row.share = r.get("share", 0.0)
            row.screen = r.get("screen", 0.0)
            row.density = r.get("density", 0.0)
            row.low = r.get("low", row.share)
            row.high = r.get("high", row.share)
            row.hint = r.get("hint", "")
            row.model = r.get("model", "")
            row.label = r.get("label", "")
            row.fix = r.get("fix", -1.0)
            row.fix_low = r.get("fix_low", 0.0)
            row.fix_high = r.get("fix_high", 0.0)
            row.through = r.get("through", 0.0)
    s.wall_seconds = report.get("wall_seconds", 0.0)
    s.probe_size = f"{report.get('width', 0)}x{report.get('height', 0)}"
    s.index = 0


def _load_heatmap(path):
    if not path or not os.path.exists(path):
        return
    img = bpy.data.images.get(HEATMAP_NAME)
    if img:
        bpy.data.images.remove(img)
    img = bpy.data.images.load(path, check_existing=False)
    img.name = HEATMAP_NAME
    img.pack()  # the temp file is deleted on the next run


def _redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type in {"PROPERTIES", "IMAGE_EDITOR"}:
                area.tag_redraw()


class RB_OT_run(bpy.types.Operator):
    bl_idname = "render_blame.run"
    bl_label = "Find What's Slow"
    bl_description = ("Render a small test copy of the scene in the background and "
                      "show which objects and materials take the most render time")

    @classmethod
    def poll(cls, context):
        return _job["proc"] is None and context.scene.camera is not None

    def execute(self, context):
        s = context.scene.render_blame
        if _job["dir"]:
            shutil.rmtree(_job["dir"], ignore_errors=True)
        out_dir = tempfile.mkdtemp(prefix="render_blame_")
        blend = os.path.join(out_dir, "copy.blend")
        # a copy, so the user's file and scene settings are never changed
        bpy.ops.wm.save_as_mainfile(filepath=blend, copy=True, relative_remap=True,
                                    check_existing=False)
        with open(os.path.join(out_dir, "settings.json"), "w") as f:
            json.dump({"resolution": s.resolution, "max_samples": s.max_samples,
                       "check_volumes": s.check_volumes}, f)
        cmd = [bpy.app.binary_path, "-b", blend, "--factory-startup",
               "--python", _probe_script(), "--",
               out_dir, os.path.join(out_dir, "settings.json")]
        _job.update(proc=subprocess.Popen(cmd), dir=out_dir, scene=context.scene.name, progress="starting")
        s.status = "running"
        s.rows.clear()
        bpy.app.timers.register(_poll_job, first_interval=0.5)
        return {"FINISHED"}


class RB_OT_cancel(bpy.types.Operator):
    bl_idname = "render_blame.cancel"
    bl_label = "Stop"
    bl_description = "Stop the test render"

    @classmethod
    def poll(cls, context):
        return _job["proc"] is not None

    def execute(self, context):
        _job["proc"].kill()
        _job["proc"] = None
        context.scene.render_blame.status = "stopped"
        return {"FINISHED"}


class RB_OT_select(bpy.types.Operator):
    bl_idname = "render_blame.select"
    bl_label = "Select"
    bl_description = "Select this object, or every object that uses this material"
    name: StringProperty()
    kind: StringProperty()

    def execute(self, context):
        if self.name == BACKGROUND:
            self.report({"INFO"}, "That is the world / background - see World properties")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        targets = []
        for ob in context.view_layer.objects:
            if self.kind in {"OBJECT", "VOLUME"}:
                hit = ob.name == self.name
            elif self.kind == "MODEL":
                top = ob
                while top.parent is not None:
                    top = top.parent
                hit = top.name == self.name
            else:
                hit = any(sl.material and sl.material.name == self.name
                          for sl in getattr(ob, "material_slots", []))
            if hit:
                targets.append(ob)
        if not targets:
            self.report({"WARNING"}, f"{self.name} is not in this view layer")
            return {"CANCELLED"}
        for ob in context.view_layer.objects:
            ob.select_set(False)
        for ob in targets:
            ob.select_set(True)
        context.view_layer.objects.active = targets[0]
        return {"FINISHED"}


class RB_OT_heatmap(bpy.types.Operator):
    bl_idname = "render_blame.heatmap"
    bl_label = "Show Heatmap"
    bl_description = "Open the cost heatmap: bright = expensive pixels"

    @classmethod
    def poll(cls, context):
        return HEATMAP_NAME in bpy.data.images

    def execute(self, context):
        img = bpy.data.images[HEATMAP_NAME]
        for area in context.screen.areas:   # reuse an open image editor
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = img
                return {"FINISHED"}
        bpy.ops.wm.window_new()
        win = context.window_manager.windows[-1]
        area = win.screen.areas[0]
        area.type = "IMAGE_EDITOR"
        area.spaces.active.image = img
        return {"FINISHED"}


# ---------------------------------------------------------------- UI

def _span(low, high):
    """'19%' or '13-15%' for a measured range."""
    lo, hi = round(max(0.0, low) * 100), round(max(0.0, high) * 100)
    return f"{lo}%" if lo == hi else f"{lo}-{hi}%"


def _visible_rows(s):
    kinds = {"OBJECT": {"OBJECT", "VOLUME"}, "MATERIAL": {"MATERIAL", "VOLUME"},
             "MODEL": {"MODEL", "VOLUME"}}[s.group_by]
    rows = [r for r in s.rows if r.kind in kinds]
    if s.sort_by == "DENSITY":
        rows.sort(key=lambda r: (r.kind == "VOLUME", r.density), reverse=True)
    else:
        rows.sort(key=lambda r: r.share, reverse=True)
    return rows


class RB_PanelDraw:
    """Shared by the viewport sidebar tab and the Render properties panel."""

    def draw(self, context):
        layout = self.layout
        s = context.scene.render_blame

        row = layout.row(align=True)
        row.scale_y = 1.4
        if _job["proc"] is not None:
            row.operator("render_blame.cancel", icon="CANCEL", text="Stop")
            layout.label(text=f"Measuring: {_job.get('progress', '')}", icon="TIME")
        else:
            row.operator("render_blame.run", icon="VIEWZOOM")
        if context.scene.camera is None:
            layout.label(text="The scene needs a camera", icon="ERROR")

        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(s, "resolution")
        col.prop(s, "max_samples")
        col.prop(s, "check_volumes")

        if s.status and s.status not in {"done", "running"}:
            layout.label(text=s.status, icon="ERROR")
        if not s.rows:
            return

        layout.separator()
        layout.row().prop(s, "group_by", expand=True)
        layout.row().prop(s, "sort_by", expand=True)

        box = layout.box()
        head = box.row()
        head.label(text="Name")
        sub = head.row()
        sub.alignment = "RIGHT"
        sub.label(text="Time    Per pixel")
        for r in _visible_rows(s)[:15]:
            line = box.split(factor=0.6, align=True)
            fixes = r.fix >= 0.0
            hot = r.kind == "VOLUME" or r.density >= HOT or (fixes and r.fix >= 0.05)
            icon = {"OBJECT": "OBJECT_DATA", "MATERIAL": "MATERIAL", "MODEL": "OUTLINER_COLLECTION",
                    "VOLUME": "OUTLINER_DATA_VOLUME"}[r.kind]
            if r.name == BACKGROUND:
                icon = "WORLD"
            left = line.row()
            left.alignment = "LEFT"
            label = r.name
            if r.kind == "OBJECT" and r.model and r.model != r.name:
                label = f"{r.name}  ({r.model})"  # 'Object_2' alone means nothing
            elif r.kind == "MODEL" and r.label:
                label = r.label
            op = left.operator("render_blame.select", text=label, icon=icon, emboss=False)
            op.name, op.kind = r.name, r.kind
            num = line.row()
            num.alignment = "RIGHT"
            num.alert = hot
            if r.kind == "VOLUME":
                # measured by hiding it, so show the spread of the repeats
                num.label(text=f"{_span(r.low, r.high)}   --")
            else:
                num.label(text=f"{r.share * 100:4.1f}%   {r.density:.1f}x")
            if r.hint and hot:
                h = box.row()
                h.separator(factor=3)
                h.label(text=r.hint, icon="INFO")
            if fixes and r.fix >= 0.02:
                h = box.row()
                h.separator(factor=3)
                h.alert = r.fix >= 0.05
                h.label(text=f"plain material saves {_span(r.fix_low, r.fix_high)}",
                        icon="TIME")
            if r.through > 0.002 and r.through > r.screen:
                h = box.row()
                h.separator(factor=3)
                h.label(text="seen through glass: time is on the glass", icon="INFO")
        if any(r.kind == "VOLUME" for r in s.rows):
            note = layout.row()
            note.label(text="Volume time is also in the rows behind it", icon="INFO")

        foot = layout.row()
        foot.label(text=f"Test render {s.probe_size}, {s.wall_seconds:.1f}s on CPU")
        layout.operator("render_blame.heatmap", icon="IMAGE_DATA")


class RB_PT_sidebar(RB_PanelDraw, bpy.types.Panel):
    bl_label = "Render Blame"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Render Blame"


class RB_PT_render_props(RB_PanelDraw, bpy.types.Panel):
    bl_label = "Render Blame"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"
    bl_options = {"DEFAULT_CLOSED"}


classes = (RB_Row, RB_Settings, RB_OT_run, RB_OT_cancel, RB_OT_select,
           RB_OT_heatmap, RB_PT_sidebar, RB_PT_render_props)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.render_blame = PointerProperty(type=RB_Settings)


def unregister():
    if _job["proc"] is not None:
        _job["proc"].kill()
        _job["proc"] = None
    if bpy.app.timers.is_registered(_poll_job):
        bpy.app.timers.unregister(_poll_job)
    del bpy.types.Scene.render_blame
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
