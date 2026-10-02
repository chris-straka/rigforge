# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Piece transfer (weight assist W3): weights for capes, hair, armor.

Verts near the body copy its weights (barycentric over the nearest
surface triangle); verts hanging away are inpainted by reusing the
nudge solver (W2) with the copied verts as pins. Deterministic: fixed
BVH queries plus fixed Jacobi counts. Uses base-mesh geometry (bind
pose), so modifiers on either object cannot skew the nearest lookup.
"""

from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import nudge as _nudge

TRANSFER_AUTO_FRACTION = 0.05  # close == within 5% of the body diagonal


def _barycentric(p, a, b, c):
    v0 = b - a
    v1 = c - a
    v2 = p - a
    d00 = v0.dot(v0)
    d01 = v0.dot(v1)
    d11 = v1.dot(v1)
    d20 = v2.dot(v0)
    d21 = v2.dot(v1)
    denom = d00 * d11 - d01 * d01
    if abs(denom) < 1e-18:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    u = 1.0 - v - w
    u, v, w = max(u, 0.0), max(v, 0.0), max(w, 0.0)
    total = u + v + w
    if total <= 0.0:
        return (1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0)
    return (u / total, v / total, w / total)


def body_diag(body_obj):
    """World-space diagonal of the body bounds (0 for degenerate)."""
    corners = [body_obj.matrix_world @ Vector(c) for c in body_obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return (
        (max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (max(zs) - min(zs)) ** 2
    ) ** 0.5


def solve_transfer(body_obj, piece_obj, max_dist=0.0, inpaint=True, k=4, iters=None):
    """Copy body weights onto the piece; returns (assignment, report).

    max_dist 0 means auto (5% of the body diagonal). Beyond max_dist
    (with inpaint) the vert is left free and the nudge solver fills it
    from the copied verts. With inpaint=False every vert copies its
    nearest triangle (plain nearest transfer, for comparison).
    """
    mesh = body_obj.data
    mesh.calc_loop_triangles()
    body_cos = [body_obj.matrix_world @ v.co for v in mesh.vertices]
    tris = mesh.loop_triangles
    bvh = BVHTree.FromPolygons(
        body_cos, [list(t.vertices) for t in tris], all_triangles=True
    )
    body_rows, _names = _nudge.read_weights(body_obj)
    diag = body_diag(body_obj)
    thresh = max_dist if max_dist > 0.0 else TRANSFER_AUTO_FRACTION * diag
    pins = {}
    scored = []  # (dist, vert_idx, row) for the no-close fallback
    for v in piece_obj.data.vertices:
        hit = bvh.find_nearest(piece_obj.matrix_world @ v.co)
        if hit is None:
            continue
        _loc, _nrm, tri_idx, dist = hit
        tv = tris[tri_idx].vertices
        weights = _barycentric(
            Vector(_loc), body_cos[tv[0]], body_cos[tv[1]], body_cos[tv[2]]
        )
        row = {}
        for vert_idx, w in zip(tv, weights, strict=True):
            if w <= 0.0:
                continue
            for bone, bw in body_rows[vert_idx].items():
                if bw > 0.0:
                    row[bone] = row.get(bone, 0.0) + w * bw
        if not row:
            continue
        scored.append((float(dist), v.index, row))
        if inpaint and dist > thresh:
            continue
        pins[v.index] = row
    seeded = False
    if not pins and scored:
        # Whole piece hangs away: seed the nearest vert, inpaint all.
        scored.sort(key=lambda t: (t[0], t[1]))
        _d, vi, row = scored[0]
        pins[vi] = row
        seeded = True
    # Transfer replaces (never blends): the piece's old groups are stale
    # seeds that would fight the pins, so solve unseeded (idempotent).
    piece_rows = [{} for _ in piece_obj.data.vertices]
    offsets, neighbors = _nudge.mesh_adjacency(piece_obj)
    assignment = _nudge.solve_nudge(
        piece_rows,
        offsets,
        neighbors,
        pins,
        k=k,
        iters=_nudge.NUDGE_ITERS if iters is None else iters,
    )
    report = {
        "close": len(pins) - (1 if seeded else 0),
        "far": len(piece_obj.data.vertices) - len(pins),
        "threshold": thresh,
        "seeded": seeded,
    }
    return assignment, report
