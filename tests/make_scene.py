"""Build tests/scene.blend: a floor, a plain box, a glass ball and a fog cube.
blender -b --factory-startup --python tests/make_scene.py"""
import bpy, os
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = 'CYCLES'; sc.cycles.samples = 128
sc.render.resolution_x, sc.render.resolution_y = 1280, 720
def mat(name, kind):
    m = bpy.data.materials.new(name)
    nt = m.node_tree; b = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    if kind == 'glass':
        b.inputs['Transmission Weight'].default_value = 1; b.inputs['Roughness'].default_value = 0.05
    if kind == 'volume':
        o = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'); nt.nodes.remove(b)
        v = nt.nodes.new('ShaderNodeVolumePrincipled'); v.inputs['Density'].default_value = 2
        nt.links.new(v.outputs[0], o.inputs['Volume'])
    return m
def add(op, name, loc, m, **kw):
    op(location=loc, **kw); o = bpy.context.object; o.name = name; o.data.materials.append(m)
add(bpy.ops.mesh.primitive_plane_add, "Floor", (0,0,0), mat("M_Floor","diffuse"), size=20)
add(bpy.ops.mesh.primitive_cube_add, "Box", (-3,0,1), mat("M_Box","diffuse"))
add(bpy.ops.mesh.primitive_uv_sphere_add, "GlassBall", (0,0,1), mat("M_Glass","glass"))
add(bpy.ops.mesh.primitive_cube_add, "FogCube", (3,0,1), mat("M_Fog","volume"))
bpy.ops.object.light_add(type='AREA', location=(0,-3,6)); bpy.context.object.data.energy = 800
bpy.ops.object.camera_add(location=(0,-11,4), rotation=(1.25,0,0)); sc.camera = bpy.context.object
sc.world = bpy.data.worlds.new("W")
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(os.path.dirname(__file__), "scene.blend"))
