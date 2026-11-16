import bpy
vl = bpy.context.view_layer
print("CYCLES VL PROPS:", [p for p in dir(vl.cycles) if 'pass' in p or 'debug' in p])
print("VL PROPS:", [p for p in dir(vl) if 'pass' in p][:60])
try:
    import OpenImageIO as oiio; print("OIIO OK", oiio.VERSION_STRING)
except Exception as e: print("OIIO no", e)
try:
    import numpy; print("numpy", numpy.__version__)
except Exception as e: print("numpy no", e)
