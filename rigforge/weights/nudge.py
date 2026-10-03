# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Nudge strokes (weight assist W2): pin a few verts, refill smoothly.

The owner selects verts (or paints a selection) with a bone picked and
pins them ("this follows the upper arm at 1.0", "not this bone").
Pins become Dirichlet constraints; every bone's field is re-solved by
fixed-count Jacobi relaxation over mesh adjacency, excluded bones are
zeroed, and each vert renormalizes to the influence budget. Fixed
iteration counts (never tolerance-based) keep output deterministic.

No bpy.ops here; callers handle mode/selection/undo. Pure mesh-data
reads plus vertex-group writes.
"""

import numpy as np

NUDGE_ITERS = 60  # Jacobi passes (fixed: deterministic, <1 s at 15k tris)
NUDGE_MIN_WEIGHT = 1e-4  # drop specks below this after renormalizing
NUDGE_SPRING = 0.9  # pin pull per pass (soft: converge near the pin)
NUDGE_FEATHER_RINGS = 5  # outward ramp rings auto-added around excludes
NUDGE_LOGIT_MIN = -12.0  # logit floor (exp = 6e-6: excluded bones
# drop from output instead of leaking past the speck cut)


def mesh_adjacency(mesh_obj):
    """CSR-ish mesh adjacency: (offsets, neighbors) over vert indices."""
    n = len(mesh_obj.data.vertices)
    edges = np.array(
        [(e.vertices[0], e.vertices[1]) for e in mesh_obj.data.edges],
        dtype=np.int64,
    )
    if len(edges) == 0:
        return np.zeros(n + 1, dtype=np.int64), np.zeros(0, dtype=np.int64)
    flat = np.concatenate([edges[:, 0], edges[:, 1]])
    other = np.concatenate([edges[:, 1], edges[:, 0]])
    counts = np.bincount(flat, minlength=n)
    offsets = np.zeros(n + 1, dtype=np.int64)
    offsets[1:] = np.cumsum(counts)
    # Stable sort by source vert keeps each vert's neighbors in edge order.
    neighbors = other[np.argsort(flat, kind="stable")]
    return offsets, neighbors


def deform_group_names(mesh_obj):
    """Names of deform bones driving the mesh, or None if no armature.

    Bones come from every Armature modifier's rig (plus an armature
    parent), so callers can tell bone weights from masks, cloth pins,
    and other non-bone vertex groups. None means the mesh is not bound
    yet: treat every group as a weight.
    """
    rigs = [
        mod.object
        for mod in mesh_obj.modifiers
        if mod.type == "ARMATURE"
        and mod.object is not None
        and mod.object.type == "ARMATURE"
    ]
    parent = mesh_obj.parent
    if parent is not None and parent.type == "ARMATURE":
        rigs.append(parent)
    if not rigs:
        return None
    return {b.name for rig in rigs for b in rig.data.bones if b.use_deform}


def read_weights(mesh_obj, only=None):
    """Per-vert {bone: weight} plus the bone order seen.

    only: set of group names to read (None reads every group).
    """
    groups = mesh_obj.vertex_groups
    names = [g.name for g in groups if only is None or g.name in only]
    lookup = {g.index: g.name for g in groups if only is None or g.name in only}
    per_vert = []
    for v in mesh_obj.data.vertices:
        row = {}
        for el in v.groups:
            name = lookup.get(el.group)
            if name is not None and el.weight > 0.0:
                row[name] = float(el.weight)
        per_vert.append(row)
    return per_vert, names


def write_weights(mesh_obj, rows, only=None):
    """Write per-vert {bone: weight} rows back, in place.

    Groups in scope (only, or every group when None) take exactly the
    rows' weights; groups outside it are never touched. Groups are
    edited, not recreated, so order and lock flags survive; names in
    the rows with no group yet (a mirror's other side) are created.
    """
    groups = mesh_obj.vertex_groups
    scope = {g.index for g in groups if only is None or g.name in only}
    stale = {}  # group index -> verts whose membership must go
    for v in mesh_obj.data.vertices:
        row = rows[v.index]
        for el in v.groups:
            if el.group in scope and groups[el.group].name not in row:
                stale.setdefault(el.group, []).append(v.index)
    for gi, verts in stale.items():
        groups[gi].remove(verts)
    cache = {}
    for vi, row in enumerate(rows):
        for name, weight in sorted(row.items(), key=lambda t: (-t[1], t[0])):
            group = cache.get(name)
            if group is None:
                group = groups.get(name) or groups.new(name=name)
                cache[name] = group
            group.add([vi], float(weight), "REPLACE")


def _to_logit(w):
    return max(NUDGE_LOGIT_MIN, float(np.log(max(w, 1e-12))))


def _presence_distance(n, offsets, neighbors, present):
    """Per-vert ring distance to the nearest vert where present (-1: none)."""
    dist = np.full(n, -1, dtype=np.int64)
    frontier = [i for i in range(n) if present[i]]
    for i in frontier:
        dist[i] = 0
    d = 0
    while frontier:
        d += 1
        nxt = []
        for i in frontier:
            for nb in neighbors[offsets[i] : offsets[i + 1]]:
                nb = int(nb)
                if dist[nb] < 0:
                    dist[nb] = d
                    nxt.append(nb)
        frontier = nxt
    return dist


def _relax(field, pinned, pinval, offsets, neighbors, iters):
    """Fixed-count Jacobi in logit space with soft pin springs."""
    deg = np.diff(offsets).astype(np.int64)
    has_nbrs = deg > 0
    src = np.repeat(np.arange(field.shape[0]), deg)
    for _ in range(iters):
        avg = np.zeros_like(field)
        np.add.at(avg, neighbors, field[src])
        avg[has_nbrs] /= deg[has_nbrs, None]
        relaxed = 0.5 * field + 0.5 * avg
        field[has_nbrs] = np.where(
            pinned[has_nbrs],
            (1.0 - NUDGE_SPRING) * relaxed[has_nbrs] + NUDGE_SPRING * pinval[has_nbrs],
            relaxed[has_nbrs],
        )
        # Isolated verts never relax; their pins snap exactly.
        iso = pinned & ~has_nbrs[:, None]
        field[iso] = pinval[iso]
    return field


def _softmax_rows(field, bones, k, cut):
    """Per-vert softmax rows; cut drops specks and caps influences at k."""
    field = field - field.max(axis=1, keepdims=True)
    np.exp(field, out=field)
    if cut:
        return _top_rows(field, bones, k)
    out = []
    for i in range(field.shape[0]):
        row = [(bones[j], float(field[i, j])) for j in range(len(bones))]
        total = sum(w for _, w in row)
        if total <= 0.0:
            out.append([])
        else:
            out.append([(b, w / total) for b, w in row])
    return out


def _top_rows(field, bones, k):
    """Cut rows: weights over the speck floor, top k (weight desc, ties
    in bone order), renormalized. Sorting is vectorized; the Python
    loop only touches the k survivors per vert."""
    order = np.argsort(-field, axis=1, kind="stable")[:, :k]
    top = np.take_along_axis(field, order, axis=1)
    keep = top > NUDGE_MIN_WEIGHT
    out = []
    for cols, vals, ok in zip(order.tolist(), top.tolist(), keep.tolist(), strict=True):
        row = [(bones[c], w) for c, w, o in zip(cols, vals, ok, strict=True) if o]
        total = sum(w for _, w in row)
        out.append([(b, w / total) for b, w in row] if total > 0.0 else [])
    return out


def solve_nudge(per_vert, offsets, neighbors, pins, k=4, iters=NUDGE_ITERS):
    """Refill weights honoring pins; returns per-vert [(bone, weight)].

    per_vert: list of {bone: weight}; pins: {vert_idx: {bone: value}}
    (value None means exclude). Jacobi runs in logit space with soft
    pin springs and the output is a per-vert softmax, so the refill is
    smooth by construction (no post-hoc renormalization cliffs) and
    always normalized. Influences per vert capped at k. With excludes,
    a second pass ramps every bone from its band-interior value to its
    seed over feather rings outside the exclude region (softmax is
    relative: ramping the excluded bone alone cannot lift a ~0
    survivor, so the whole row is feathered).
    """
    n = len(per_vert)
    bones = sorted(
        {b for row in per_vert for b in row} | {b for p in pins.values() for b in p}
    )
    if not bones:
        return [[] for _ in range(n)]
    col = {b: j for j, b in enumerate(bones)}
    field = np.full((n, len(bones)), NUDGE_LOGIT_MIN)
    for i, row in enumerate(per_vert):
        for b, w in row.items():
            field[i, col[b]] = _to_logit(w)
    pinned = np.zeros((n, len(bones)), dtype=bool)
    pinval = np.full((n, len(bones)), NUDGE_LOGIT_MIN)
    excl_by_bone = {}
    dist_cache = {}
    for i, spec in pins.items():
        for b, v in spec.items():
            pinned[i, col[b]] = True
            pinval[i, col[b]] = NUDGE_LOGIT_MIN if v is None else _to_logit(v)
            if v is None:
                excl_by_bone.setdefault(col[b], set()).add(i)
        # Complement pass: value pins are absolute promises but softmax
        # is relative, so pin the other bones too (skipping artist pins).
        for b, v in spec.items():
            if v is None:
                continue
            others = [
                j2 for j2 in range(len(bones)) if j2 != col[b] and not pinned[i, j2]
            ]
            if not others:
                continue
            if v >= 0.999:
                # Exclusive pin: fully-follows means the rest go to 0.
                for j2 in others:
                    pinned[i, j2] = True
                    pinval[i, j2] = NUDGE_LOGIT_MIN
                continue
            seed_row = per_vert[i]
            o = sum(seed_row.get(bones[j2], 0.0) for j2 in others)
            if o > 1e-9:
                for j2 in others:
                    target = seed_row.get(bones[j2], 0.0) * (1.0 - v) / o
                    pinned[i, j2] = True
                    pinval[i, j2] = _to_logit(target)
            else:
                # No other seeds here: the nearest-present bones carry
                # the complement (a 0.7 pin summons help, not silence).
                best, bestd = [], None
                for j2 in others:
                    if j2 not in dist_cache:
                        present = np.zeros(n, dtype=bool)
                        for ii, row in enumerate(per_vert):
                            if row.get(bones[j2], 0.0) > 0.0:
                                present[ii] = True
                        dist_cache[j2] = _presence_distance(
                            n, offsets, neighbors, present
                        )
                    d = dist_cache[j2][i]
                    if d < 0:
                        continue
                    if bestd is None or d < bestd:
                        best, bestd = [j2], d
                    elif d == bestd:
                        best.append(j2)
                for j2 in best:
                    pinned[i, j2] = True
                    pinval[i, j2] = _to_logit((1.0 - v) / len(best))
    # Warm start: fully unseeded verts begin at the pins' mean row
    # instead of the logit floor (far inpaint regions would otherwise
    # need hundreds of passes to crawl up from exp(-12)). Seeded
    # verts keep their seeds (W2 behavior unchanged).
    empty = [i for i, row in enumerate(per_vert) if not row]
    if empty:
        for j in range(len(bones)):
            vals = pinval[pinned[:, j], j]
            if len(vals):
                field[empty, j] = _to_logit(float(np.exp(vals).mean()))
    _relax(field, pinned, pinval, offsets, neighbors, iters)
    if not excl_by_bone:
        return _softmax_rows(field, bones, k, cut=True)
    # Pass 1 found the band-interior redistribution; pass 2 feathers
    # every bone from its band value to its seed (warm start).
    band_of = [dict(r) for r in _softmax_rows(field, bones, k, cut=False)]
    for j, sources in excl_by_bone.items():
        seen = set(sources)
        src_of = {i: i for i in sources}
        frontier = list(sources)
        for ring in range(1, NUDGE_FEATHER_RINGS + 1):
            t = ring / (NUDGE_FEATHER_RINGS + 1)
            nxt = []
            for i in frontier:
                for nb in neighbors[offsets[i] : offsets[i + 1]]:
                    nb = int(nb)
                    if nb in seen:
                        continue
                    seen.add(nb)
                    src_of[nb] = src_of[i]
                    if nb in sources:
                        continue
                    band = band_of[src_of[nb]]
                    seed_row = per_vert[nb]
                    for j2, b2 in enumerate(bones):
                        if pinned[nb, j2]:
                            continue
                        seed_w = seed_row.get(b2, 0.0)
                        if j2 == j:
                            target = seed_w * t  # band side is 0 (excluded)
                        else:
                            target = (1.0 - t) * band.get(b2, 0.0) + t * seed_w
                        pinval[nb, j2] = _to_logit(target)
                        pinned[nb, j2] = True
                    nxt.append(nb)
            frontier = nxt
    _relax(field, pinned, pinval, offsets, neighbors, iters)
    return _softmax_rows(field, bones, k, cut=True)


def rows_to_assignment(rows):
    """Dict rows -> apply_nudge assignment (weight desc, name asc)."""
    return [sorted(row.items(), key=lambda t: (-t[1], t[0])) for row in rows]


def apply_nudge(mesh_obj, assignment):
    """Replace the mesh's vertex groups with the solved assignment."""
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
