# Headless Godot import check for rigforge GLBs (game-export depth).
# Usage: Godot --headless --script tools/godot_import_check.gd
#            --quit-after 1 -- <glb> [<glb> ...]
# Prints one GODOT_STAT line per file; exits 1 if any file fails to
# load or yields no skeleton. Called via tools/godot_import_check.py.
extends SceneTree

func _init():
    var failed = false
    for path in OS.get_cmdline_user_args():
        var doc = GLTFDocument.new()
        var state = GLTFState.new()
        var err = doc.append_from_file(path, state)
        if err != OK:
            print("LOAD_FAIL path=", path, " err=", err)
            failed = true
            continue
        var scene = doc.generate_scene(state)
        var bones = 0
        var skels = []
        _collect(scene, Skeleton3D, skels)
        for s in skels:
            bones += s.get_bone_count()
        if bones == 0:
            print("LOAD_FAIL path=", path, " err=no-skeleton")
            failed = true
            continue
        var verts = 0
        var maxinf = 0
        var meshes = []
        _collect(scene, MeshInstance3D, meshes)
        for mi in meshes:
            var mesh = mi.mesh
            if mesh == null:
                continue
            for si in range(mesh.get_surface_count()):
                var arr = mesh.surface_get_arrays(si)
                if arr.size() <= Mesh.ARRAY_WEIGHTS:
                    continue
                var w = arr[Mesh.ARRAY_WEIGHTS]
                var vcount = arr[Mesh.ARRAY_VERTEX].size()
                verts += vcount
                for vi in range(vcount):
                    var n = 0
                    for k in range(4):
                        if absf(float(w[vi * 4 + k])) > 0.0001:
                            n += 1
                    maxinf = maxi(maxinf, n)
        var size = FileAccess.get_file_as_bytes(path).size()
        print("GODOT_STAT path=", path, " bones=", bones, " verts=", verts,
            " maxinf=", maxinf, " bytes=", size)
    quit(1 if failed else 0)

func _collect(node, klass, out):
    if node == null:
        return
    if is_instance_of(node, klass):
        out.append(node)
    for c in node.get_children():
        _collect(c, klass, out)
