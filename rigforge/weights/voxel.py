# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Geodesic voxel binding (weight assist W1).

Robust automatic weights after Dionne & de Lasa 2013: voxelize the
closed mesh interior, compute per-bone geodesic distances through the
voxel volume (Dijkstra from each bone's seeds, bounded by a per-bone
cutoff), turn the k nearest distances per voxel into smooth weights
with a per-voxel-relative kernel, smooth each bone's field over the
grid, then sample at the mesh verts. Paths stay inside flesh, so
weights never bleed across air gaps (armpit, groin, fingers) and the
solve works on any closed mesh, however messy its tessellation.

Determinism: bones iterate in sorted-name order, Dijkstra breaks ties
by voxel index, all math is numpy/vectorized with fixed seeds — same
inputs give identical groups. No bpy.ops here (mode/selection belong
to the caller); mesh data is read raw with matrix_world applied, so
callers should pass transforms-applied meshes.
"""

import heapq
import os

import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

K_DEFAULT = 4  # bones per voxel/vert (Godot/mobile 4-influence budget)
BLEND_REF = 0.016  # reference blend scale (m): smoothing passes are
# (BLEND_REF / cell)^2, so the physical blend width stays constant
# across grid resolutions (calibrated: 6 passes at 6.5 mm cells).
SMOOTH_MULT = 3.0  # W1b: characters want ~3x the tube blend (wider
# physical target; tube stays green with margin).
POWER_DEFAULT = 6.0  # per-voxel-relative kernel exponent (W1b: crisp
# commitment; pre-smooth transitions are razor-thin, smoothing re-spreads)
MAX_GRID_CELLS = 8_000_000  # voxelize guard: pass explicit cell past this
DECAY_LEN_DIV = 4.0  # per-bone decay scale = length / div ...
DECAY_S_MIN = 0.02  # ... clamped to [s_min, s_max] (m). Env RF_VOX_LEN_DIV,
DECAY_S_MAX = 0.10  # RF_VOX_S_MIN/S_MAX override; RF_VOX_DECAY=0 disables.
SEED_R_CELLS = 1.5  # seed voxels within this many cells of a bone segment
SEED_OUT_CELLS = 4.0  # past this the bone is out of flesh: excluded
# (no mush from 20 cm-away bones); inside it the nearest voxels seed
# (slop for slightly misplaced bones and sub-cell bones).
CUTOFF_LEN_MULT = 6.0  # per-bone Dijkstra cutoff vs bone length ...
CUTOFF_CELL_MULT = 8.0  # ... vs cell size (whichever binds wider) ...
CUTOFF_MAX = 0.35  # ... capped at this (HLL scale; torso-spanning)
RAY_EPS = 1e-5  # ray-walk step past each shell crossing
RAY_TILT = Vector((1e-4, 2e-4, 1.0)).normalized()  # dodge edge-on rays
MIN_INFLUENCE = 0.0001  # mirrors tools/auto_rig.py (same joint assignment)


def deform_bone_names(armature_obj):
    """Sorted names of deforming bones (DEF- on generated rigs, all on plain)."""
    return sorted(b.name for b in armature_obj.data.bones if b.use_deform)


def world_verts(mesh_obj):
    """Mesh vertex coordinates in world space (n, 3)."""
    mat = mesh_obj.matrix_world
    return np.array(
        [tuple(mat @ v.co) for v in mesh_obj.data.vertices], dtype=np.float64
    )


def world_tris(mesh_obj):
    """Mesh loop-triangle corner indices (t, 3), computed deterministically."""
    mesh = mesh_obj.data
    mesh.calc_loop_triangles()
    return np.array([tuple(t.vertices) for t in mesh.loop_triangles], dtype=np.int64)


def default_cell(world_bb):
    """Voxel pitch: bbox diagonal / 160 (joint blends resolve 3+ cells)."""
    lo = np.min(world_bb, axis=0)
    hi = np.max(world_bb, axis=0)
    return float(np.linalg.norm(hi - lo)) / 160.0


def smooth_for_cell(cell, diag=None):
    """Laplacian passes for a constant physical blend width at this pitch.

    Capped so the blend never exceeds ~1/8 of the mesh extent: past
    that the field is mesh-global mush, and at tiny scales the raw
    (BLEND_REF / cell)^2 count runs to millions of passes (a hang).
    """
    n = max(2, round((BLEND_REF / cell) ** 2))
    if diag:
        n = min(n, max(4, round((diag / 8.0 / cell) ** 2)))
    return min(n, 500)


def voxelize(mesh_obj, cell, pad_cells=2):
    """Solid voxelization of a closed mesh; returns the grid dict.

    Parity-fills +z columns over the world bbox (BVH ray walk, even-odd
    between crossings). Keys: origin, cell, shape, interior (flat bool),
    centers (m, 3) of interior voxels, index (flat int32, voxel -> row).
    """
    verts = world_verts(mesh_obj)
    tris = world_tris(mesh_obj)
    tree = BVHTree.FromPolygons(
        [Vector(tuple(v)) for v in verts], [tuple(t) for t in tris.tolist()]
    )
    lo = np.min(verts, axis=0) - pad_cells * cell
    hi = np.max(verts, axis=0) + pad_cells * cell
    shape = np.maximum(1, np.ceil((hi - lo) / cell).astype(int))
    if int(np.prod(shape)) > MAX_GRID_CELLS:
        raise ValueError(
            f"voxel grid {shape.tolist()} exceeds {MAX_GRID_CELLS} cells; "
            "pass an explicit coarser cell"
        )
    origin = lo
    nx, ny, nz = (int(v) for v in shape)
    interior = np.zeros(nx * ny * nz, dtype=bool)
    direction = RAY_TILT
    for ix in range(nx):
        x = origin[0] + (ix + 0.5) * cell
        for iy in range(ny):
            y = origin[1] + (iy + 0.5) * cell
            start = Vector((x, y, origin[2] - cell))
            top = origin[2] + nz * cell + cell
            hits = []
            point = start
            step, stall, iters = RAY_EPS, 0, 0
            while iters < 128:
                iters += 1
                found = tree.ray_cast(point, direction)
                if found[0] is None:
                    break
                loc = found[0]
                if hits and abs(loc.z - hits[-1]) < 1e-9:
                    # Same shell re-hit (float stall): grow the step.
                    stall += 1
                    step = RAY_EPS * (10.0 ** min(stall, 6))
                else:
                    hits.append(loc.z)
                    stall, step = 0, RAY_EPS
                point = loc + direction * step
                if point.z > top:
                    break
            hits.sort()
            for z0, z1 in zip(hits[::2], hits[1::2], strict=False):
                k0 = max(0, int(np.floor((z0 - origin[2]) / cell)))
                k1 = min(nz - 1, int(np.floor((z1 - origin[2]) / cell)))
                for k in range(k0, k1 + 1):
                    interior[(k * ny + iy) * nx + ix] = True
    centers = _grid_centers(origin, cell, (nx, ny, nz), interior)
    index = np.full(nx * ny * nz, -1, dtype=np.int32)
    index[np.flatnonzero(interior)] = np.arange(int(np.count_nonzero(interior)))
    return {
        "origin": origin,
        "cell": cell,
        "shape": (nx, ny, nz),
        "interior": interior,
        "centers": centers,
        "index": index,
    }


def _grid_centers(origin, cell, shape, interior):
    nx, ny, _nz = shape
    flat = np.flatnonzero(interior)
    iz = flat // (nx * ny)
    rem = flat % (nx * ny)
    iy = rem // nx
    ix = rem % nx
    return np.stack(
        (
            origin[0] + (ix + 0.5) * cell,
            origin[1] + (iy + 0.5) * cell,
            origin[2] + (iz + 0.5) * cell,
        ),
        axis=1,
    )


def interior_neighbors(grid):
    """6-neighborhood over interior voxels: list per row of neighbor rows."""
    nx, ny, nz = grid["shape"]
    index = grid["index"]
    flat = np.flatnonzero(grid["interior"])
    iz = flat // (nx * ny)
    rem = flat % (nx * ny)
    iy = rem // nx
    ix = rem % nx
    out = []
    for x, y, z in zip(ix.tolist(), iy.tolist(), iz.tolist(), strict=True):
        nbrs = []
        if x > 0:
            nbrs.append(index[(z * ny + y) * nx + x - 1])
        if x < nx - 1:
            nbrs.append(index[(z * ny + y) * nx + x + 1])
        if y > 0:
            nbrs.append(index[(z * ny + y - 1) * nx + x])
        if y < ny - 1:
            nbrs.append(index[(z * ny + y + 1) * nx + x])
        if z > 0:
            nbrs.append(index[((z - 1) * ny + y) * nx + x])
        if z < nz - 1:
            nbrs.append(index[((z + 1) * ny + y) * nx + x])
        out.append([int(n) for n in nbrs if n >= 0])
    return out


def segment_distances(centers, p0, p1):
    """Per-row distance from voxel centers to the bone segment."""
    axis = p1 - p0
    denom = float(np.dot(axis, axis))
    if denom < 1e-12:
        return np.linalg.norm(centers - p0, axis=1)
    t = np.clip((centers - p0) @ axis / denom, 0.0, 1.0)
    return np.linalg.norm(centers - (p0 + t[:, None] * axis), axis=1)


def dijkstra_bounded(neighbors, seeds, cutoff, step):
    """Geodesic distances from seeds over the voxel graph (inf past cutoff).

    neighbors: interior adjacency; seeds: row indices at distance 0.
    Tie-breaks by voxel index, so the field is deterministic.
    """
    dist = np.full(len(neighbors), np.inf)
    heap = []
    for s in seeds:
        dist[s] = 0.0
        heap.append((0.0, int(s)))
    heapq.heapify(heap)
    while heap:
        d, i = heapq.heappop(heap)
        if d > dist[i] or d > cutoff:
            continue
        nxt = d + step
        if nxt > cutoff:
            continue
        for j in neighbors[i]:
            if nxt < dist[j]:
                dist[j] = nxt
                heapq.heappush(heap, (nxt, j))
    return dist


def bone_world_segment(armature_obj, bone_name):
    """Bone head/tail in world space ((3,), (3,))."""
    mat = armature_obj.matrix_world
    bone = armature_obj.data.bones[bone_name]
    return np.array(tuple(mat @ bone.head_local)), np.array(
        tuple(mat @ bone.tail_local)
    )


def _smooth_field(dense, interior, shape, iters):
    """Laplacian smoothing of one bone field (interior only, dense grid)."""
    nx, ny, nz = shape
    vol = dense.reshape((nz, ny, nx))
    mask = interior.reshape((nz, ny, nx))
    for _ in range(iters):
        acc = np.zeros_like(vol)
        cnt = np.zeros_like(vol)
        for axis, delta in (
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (2, 1),
            (2, -1),
        ):
            shifted = np.roll(vol, delta, axis=axis)
            shifted_m = np.roll(mask, delta, axis=axis)
            # np.roll wraps; zero the wrapped face.
            face = [slice(None)] * 3
            face[axis] = 0 if delta > 0 else -1
            shifted[tuple(face)] = 0.0
            shifted_m[tuple(face)] = False
            acc += np.where(shifted_m, shifted, 0.0)
            cnt += shifted_m.astype(np.float64)
        vol = np.where(mask & (cnt > 0), (vol + acc) / (1.0 + cnt), vol)
    dense = vol.reshape(-1)
    dense[~interior] = 0.0
    return dense


def _trilinear_rows(grid, kd, point):
    """Enclosing cell corners (voxel rows) + trilinear weights for a point.

    Exterior corners borrow the nearest interior voxel, so surface
    sampling is C0-continuous: no stairstep shear where the mesh
    crosses voxel boundaries (the classic flip source).
    """
    origin, cell = grid["origin"], grid["cell"]
    nx, ny, nz = grid["shape"]
    index = grid["index"]
    g = (point - origin) / cell - 0.5
    upper = [max(nx - 2, 0), max(ny - 2, 0), max(nz - 2, 0)]
    lo = np.clip(np.floor(g).astype(int), 0, upper)
    frac = np.clip(g - lo, 0.0, 1.0)
    out = []
    for dx in (0, 1):
        wx = frac[0] if dx else 1.0 - frac[0]
        for dy in (0, 1):
            wy = frac[1] if dy else 1.0 - frac[1]
            for dz in (0, 1):
                tw = wx * wy * (frac[2] if dz else 1.0 - frac[2])
                if tw <= 0.0:
                    continue
                cx = min(lo[0] + dx, nx - 1)
                cy = min(lo[1] + dy, ny - 1)
                cz = min(lo[2] + dz, nz - 1)
                row = int(index[(cz * ny + cy) * nx + cx])
                if row < 0:
                    corner = origin + (np.array([cx, cy, cz]) + 0.5) * cell
                    _, row, _ = kd.find(Vector(tuple(corner)))
                out.append((row, float(tw)))
    return out


def bind_weights(
    mesh_obj,
    armature_obj,
    bone_names=None,
    cell=None,
    source_obj=None,
    k=K_DEFAULT,
    smooth_iters=None,
    power=None,
):
    """Geodesic voxel weights; returns (assignment, report).

    assignment: per-vert list of (bone_name, weight) sorted by weight
    desc (weights normalized, influences <= k). source_obj (default the
    mesh): the closed mesh to voxelize — pass the envelope remesh when
    binding a stacked-shell production mesh. smooth_iters None means
    automatic (constant physical blend width for the grid pitch).
    report: grid stats, per-bone seed/cutoff info, fallback counts.
    """
    if bone_names is None:
        bone_names = deform_bone_names(armature_obj)
    else:
        bone_names = sorted(bone_names)
    if not bone_names:
        raise ValueError("no deforming bones on the armature")
    k = int(os.environ.get("RF_VOX_K", str(k)))
    source = source_obj or mesh_obj
    if cell is None:
        cell = default_cell(world_verts(source))
    if power is None:
        power = float(os.environ.get("RF_VOX_POWER", str(POWER_DEFAULT)))
    if smooth_iters is None:
        sco = world_verts(source)
        diag = float(np.linalg.norm(sco.max(axis=0) - sco.min(axis=0)))
        smooth_iters = smooth_for_cell(cell, diag)
        mult = float(os.environ.get("RF_VOX_SMOOTH_MULT", str(SMOOTH_MULT)))
        smooth_iters = max(1, round(smooth_iters * mult))
    grid = voxelize(source, cell)
    centers = grid["centers"]
    interior = grid["interior"]
    shape = grid["shape"]
    flat_n = int(interior.size)
    m = int(centers.shape[0])
    if m == 0 or (m < 64 and flat_n > 4096):
        # Big grid + ~no interior = parity leaked through an open shell
        # (small grids with little interior are just tiny meshes).
        raise ValueError("voxelization found no interior (mesh not closed?)")
    neighbors = interior_neighbors(grid)

    segments = [bone_world_segment(armature_obj, n) for n in bone_names]
    bone_lens = np.array([float(np.linalg.norm(p1 - p0)) for p0, p1 in segments])
    decay_on = os.environ.get("RF_VOX_DECAY", "1") != "0"
    len_div = float(os.environ.get("RF_VOX_LEN_DIV", str(DECAY_LEN_DIV)))
    s_min = float(os.environ.get("RF_VOX_S_MIN", str(DECAY_S_MIN)))
    s_max = float(os.environ.get("RF_VOX_S_MAX", str(DECAY_S_MAX)))
    scales = np.clip(bone_lens / len_div, s_min, s_max)
    dists = np.full((m, len(bone_names)), np.inf)
    bone_info = []
    for b, (name, (p0, p1)) in enumerate(zip(bone_names, segments, strict=True)):
        seg_d = segment_distances(centers, p0, p1)
        seeds = np.flatnonzero(seg_d <= SEED_R_CELLS * cell).tolist()
        seeded = f"r<={SEED_R_CELLS}*cell"
        if not seeds:
            near5 = np.argsort(seg_d, kind="stable")[:5]
            if seg_d[near5].max() <= SEED_OUT_CELLS * cell:
                seeds = near5.tolist()
                seeded = "nearest5"
            else:
                bone_info.append(
                    {
                        "bone": name,
                        "seeds": 0,
                        "seeded": "out-of-range",
                        "cutoff": None,
                        "reached": 0,
                    }
                )
                continue
        bone_len = float(np.linalg.norm(p1 - p0))
        cutoff = min(
            max(CUTOFF_LEN_MULT * bone_len, CUTOFF_CELL_MULT * cell), CUTOFF_MAX
        )
        dists[:, b] = dijkstra_bounded(neighbors, seeds, cutoff, cell)
        reached = int(np.count_nonzero(dists[:, b] <= cutoff))
        bone_info.append(
            {
                "bone": name,
                "seeds": len(seeds),
                "seeded": seeded,
                "cutoff": round(cutoff, 4),
                "reached": reached,
            }
        )

    # Per-voxel-relative kernel over the k nearest in-range bones.
    flat_inside = np.flatnonzero(interior)
    order = np.argsort(dists, axis=1, kind="stable")[:, :k]
    nearest = np.take_along_axis(dists, order, axis=1)
    usable = np.isfinite(nearest)
    rows = np.flatnonzero(usable.any(axis=1))
    # Per-bone values over interior rows only; each bone is densified
    # for smoothing one at a time so big rigs never hold bones x grid.
    stacked = np.zeros((len(bone_names), m))
    for i in rows.tolist():
        cols = order[i][usable[i]]
        if cols.size == 0:
            continue
        d = nearest[i][usable[i]]
        span = d.max() - d.min()
        if span < 1e-9:
            w = np.full_like(d, 1.0 / d.size)
        else:
            w = ((d.max() - d) / span) ** power
            w = w / w.sum()
        if decay_on:
            # Cauchy falloff per bone scale: far bones fade instead of
            # sharing span-normalized mush (W1b: kills distant splits).
            w = w / (1.0 + (d / scales[cols]) ** 2)
            w = w / w.sum()
        for col, weight in zip(cols.tolist(), w.tolist(), strict=True):
            stacked[col, i] = weight
    del dists, order, nearest, usable, rows
    for b in range(len(bone_names)):
        if np.count_nonzero(stacked[b]) == 0:
            continue
        dense = np.zeros(flat_n)
        dense[flat_inside] = stacked[b]
        stacked[b] = _smooth_field(dense, interior, shape, smooth_iters)[flat_inside]

    # Re-sparsify to k per voxel after smoothing, then sample at verts.
    order2 = np.argsort(-stacked, axis=0, kind="stable")[:k]
    values = np.take_along_axis(stacked, order2, axis=0)
    keep = values > 1e-6
    sums = np.where(keep, values, 0.0).sum(axis=0)
    kd = KDTree(m)
    for i, co in enumerate(centers.tolist()):
        kd.insert(Vector(co), i)
    kd.balance()
    mesh_co = world_verts(mesh_obj)
    assignment = []
    filled_fallback = 0
    for co in mesh_co.tolist():
        acc: dict[int, float] = {}
        for row, tw in _trilinear_rows(grid, kd, np.array(co)):
            if tw <= 0.0:
                continue
            cols = order2[:, row][keep[:, row]]
            if cols.size == 0 or sums[row] <= 0.0:
                continue
            w = values[:, row][keep[:, row]] / sums[row]
            for col, wgt in zip(cols.tolist(), w.tolist(), strict=True):
                acc[col] = acc.get(col, 0.0) + tw * wgt
        if not acc:
            # Outside every field (should not happen on closed meshes):
            # nearest bone segment in Euclidean space, weight 1.
            dmin, best = np.inf, 0
            for b, (p0, p1) in enumerate(segments):
                d = segment_distances(np.array([co]), p0, p1)[0]
                if d < dmin:
                    dmin, best = d, b
            filled_fallback += 1
            assignment.append([(bone_names[best], 1.0)])
            continue
        top = sorted(acc.items(), key=lambda t: -t[1])[:k]
        total = sum(w for _, w in top)
        assignment.append([(bone_names[c], w / total) for c, w in top])
    report = {
        "cell": cell,
        "grid": list(shape),
        "interior_voxels": m,
        "bones": len(bone_names),
        "k": k,
        "smooth_iters": smooth_iters,
        "power": power,
        "decay": {
            "on": decay_on,
            "len_div": len_div,
            "s_min": s_min,
            "s_max": s_max,
        },
        "fallback_verts": filled_fallback,
        "bone_info": bone_info,
    }
    return assignment, report


def apply_weights(mesh_obj, assignment):
    """Replace the mesh's vertex groups with the assignment (REPLACE)."""
    groups = mesh_obj.vertex_groups
    groups.clear()
    cache = {}
    for vi, pairs in enumerate(assignment):
        for name, weight in pairs:
            group = cache.get(name)
            if group is None:
                group = groups.new(name=name)
                cache[name] = group
            group.add([vi], float(weight), "REPLACE")


def fill_unassigned(mesh_obj):
    """Copy nearest-weighted-vert weights onto verts with no usable entry.

    Returns the number of verts filled. Mirrors tools/auto_rig.py: without
    this, the exporter parks such verts on a synthesized non-DEF joint.
    """
    verts = mesh_obj.data.vertices
    good_idx = [
        v.index for v in verts if any(el.weight > MIN_INFLUENCE for el in v.groups)
    ]
    bad = [
        v.index for v in verts if not any(el.weight > MIN_INFLUENCE for el in v.groups)
    ]
    if not bad or not good_idx:
        return 0
    kd = KDTree(len(good_idx))
    for i, vi in enumerate(good_idx):
        kd.insert(verts[vi].co, i)
    kd.balance()
    groups = mesh_obj.vertex_groups
    for vi in bad:
        _, i, _ = kd.find(verts[vi].co)
        for el in verts[good_idx[i]].groups:
            if el.weight > MIN_INFLUENCE:
                groups[el.group].add([vi], el.weight, "REPLACE")
    return len(bad)


def ensure_armature_modifier(mesh_obj, armature_obj):
    """Armature modifier onto the rig (kept if already present)."""
    for mod in mesh_obj.modifiers:
        if mod.type == "ARMATURE" and mod.object == armature_obj:
            return mod
    mod = mesh_obj.modifiers.new("Armature", "ARMATURE")
    mod.object = armature_obj
    return mod
