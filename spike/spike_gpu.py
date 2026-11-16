import bpy, os, time
import numpy as np, OpenImageIO as oiio
out = os.path.abspath("spike_gpu.exr")
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = 'CYCLES'; prefs=bpy.context.preferences.addons['cycles'].preferences; prefs.compute_device_type='METAL'; prefs.get_devices(); [setattr(d,'use',True) for d in prefs.devices]; print('DEVICES',[(d.name,d.type) for d in prefs.devices]); sc.cycles.device = 'GPU'
sc.cycles.samples = 64; sc.cycles.use_adaptive_sampling = True; sc.cycles.use_denoising = False
sc.render.resolution_x, sc.render.resolution_y = 320, 180
def mat(name, kind):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; b = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    if kind == 'glass': b.inputs['Transmission Weight'].default_value = 1; b.inputs['Roughness'].default_value = 0.05
    if kind == 'volume':
        out_n = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'); nt.nodes.remove(b)
        v = nt.nodes.new('ShaderNodeVolumePrincipled'); v.inputs['Density'].default_value = 2
        nt.links.new(v.outputs[0], out_n.inputs['Volume'])
    return m
objs = []
def add(op, name, loc, m, **kw):
    op(location=loc, **kw); o = bpy.context.object; o.name = name; o.data.materials.append(m); objs.append(o)
add(bpy.ops.mesh.primitive_plane_add, "Floor", (0,0,0), mat("M_Floor","diffuse"), size=20)
add(bpy.ops.mesh.primitive_cube_add, "Box", (-3,0,1), mat("M_Box","diffuse"))
add(bpy.ops.mesh.primitive_uv_sphere_add, "GlassBall", (0,0,1), mat("M_Glass","glass"))
add(bpy.ops.mesh.primitive_cube_add, "FogCube", (3,0,1), mat("M_Fog","volume"))
for i,o in enumerate(objs, 1): o.pass_index = i
bpy.ops.object.light_add(type='AREA', location=(0,-3,6)); bpy.context.object.data.energy = 800
bpy.ops.object.camera_add(location=(0,-11,4), rotation=(1.25,0,0)); sc.camera = bpy.context.object
w = bpy.data.worlds.new("W"); sc.world = w
vl = sc.view_layers[0]
vl.cycles.pass_render_time = True; vl.cycles.pass_debug_sample_count = True
vl.use_pass_object_index = True; vl.use_pass_cryptomatte_object = True
sc.render.image_settings.media_type = 'MULTI_LAYER_IMAGE'; sc.render.filepath = out
t = time.time(); bpy.ops.render.render(write_still=True); print("RENDER_S", round(time.time()-t,2))
img = oiio.ImageInput.open(out); layers = {}
sub = 0
while img.seek_subimage(sub, 0):
    sp = img.spec(); px = img.read_image(format=oiio.FLOAT)
    for i,c in enumerate(sp.channelnames): layers[c] = px[..., i]
    sub += 1
img.close(); print("CHANNELS", list(layers))
def get(s):
    k = [c for c in layers if s in c]; return (layers[k[0]] if k else None), k
rt, n1 = get("Render Time"); sc_, n2 = get("Sample Count"); oi, n3 = get("Object Index")
print("USING", n1, n2, n3)
names = {0:"(background)", **{o.pass_index:o.name for o in objs}}
oi = np.rint(oi).astype(int)
for label, arr in (("TIME", rt), ("SAMPLES", sc_)):
    if arr is None: print(label, "missing"); continue
    tot = arr.sum()
    rows = sorted(((arr[oi==k].sum()/tot*100, names.get(k,k), int((oi==k).sum())) for k in np.unique(oi)), reverse=True)
    print(label); [print(f"  {p:5.1f}%  {n:12s} pixels={c}") for p,n,c in rows]
