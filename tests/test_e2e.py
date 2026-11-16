"""End-to-end: register the add-on, press Find What's Slow on tests/scene.blend,
and check the answer.  Run with tests/run_tests.sh"""
import os, sys, time
import bpy
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import render_blame as rb
rb.register()
fails = []
def check(ok, msg):
    print(("PASS " if ok else "FAIL ") + msg); ok or fails.append(msg)

s = bpy.context.scene.render_blame
before = (bpy.context.scene.render.resolution_percentage, bpy.context.scene.cycles.samples,
          bpy.context.scene.cycles.device, bpy.context.view_layer.cycles.pass_render_time)
assert bpy.ops.render_blame.run() == {"FINISHED"}
rb._job["proc"].wait(timeout=240)
rb._poll_job()          # timers do not tick in background mode, so poll by hand
check(s.status == "done", f"probe finished (status: {s.status})")
rows = {(r.kind, r.name): r for r in s.rows}
glass, box, floor = (rows.get(("OBJECT", n)) for n in ("GlassBall", "Box", "Floor"))
check(glass is not None and box is not None, "objects reported")
check(glass and glass.density > 1.5, f"glass ball is hot per pixel ({glass and glass.density:.2f}x)")
check(box and box.density < 1.3, f"plain box is not hot ({box and box.density:.2f}x)")
check(floor and floor.share > glass.share, "floor has the biggest share (it fills the frame)")
check("glass" in (glass.hint if glass else ""), "glass ball hint mentions glass")
check(("MATERIAL", "M_Glass") in rows, "materials reported")
fog = rows.get(("VOLUME", "FogCube"))
check(fog is not None and fog.share > 0.03, f"fog cube measured by hiding it ({fog and fog.share*100:.1f}%)")
check("Render Blame Heatmap" in bpy.data.images, "heatmap loaded")
after = (bpy.context.scene.render.resolution_percentage, bpy.context.scene.cycles.samples,
         bpy.context.scene.cycles.device, bpy.context.view_layer.cycles.pass_render_time)
check(before == after, "user's scene settings untouched")
s.sort_by = "DENSITY"
top = rb._visible_rows(s)
check(top[0].kind == "VOLUME" and top[1].name == "GlassBall", "cost-per-pixel sort puts volume, then glass first")
check(bpy.ops.render_blame.select(name="M_Glass", kind="MATERIAL") == {"FINISHED"}
      and bpy.context.view_layer.objects.active.name == "GlassBall", "selecting a material selects its object")
rb.unregister()
print(f"\n{len(fails)} failed" if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
