# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Cleanup ops (weight assist W4): limit, normalize, prune, mirror, smooth.

Pure row transforms over per-vert {bone: weight} dicts (same shape as
the nudge solver's); the operator reads groups, transforms, and writes
back. All deterministic: sorted bones, fixed pass counts.
"""

from mathutils.kdtree import KDTree

CLEANUP_SMOOTH_BLEND = 0.5  # neighbor mean vs self per smooth pass

_MIRROR_SUFFIXES = ((".L", ".R"), (".R", ".L"), ("_L", "_R"), ("_R", "_L"))


def mirror_bone(name):
    """Left<->right bone name (.L/.R, _L/_R); unmarked names unchanged."""
    for left, right in _MIRROR_SUFFIXES:
        if name.endswith(left):
            return name[: -len(left)] + right
    return name


def _renorm(row):
    total = sum(row.values())
    if total <= 0.0:
        return {}
    return {b: w / total for b, w in row.items()}


def limit_rows(rows, k=4):
    """Keep the top-k influences per vert (weight desc, name asc), renorm."""
    out = []
    for row in rows:
        if len(row) <= k:
            out.append(_renorm(row) if row else {})
            continue
        top = sorted(row.items(), key=lambda t: (-t[1], t[0]))[:k]
        out.append(_renorm(dict(top)))
    return out


def normalize_rows(rows):
    """Scale every row to sum 1 (empties stay empty)."""
    return [_renorm(row) if row else {} for row in rows]


def prune_rows(rows, threshold=0.01):
    """Drop influences under threshold, renorm; stranded rows keep max."""
    out = []
    for row in rows:
        if not row:
            out.append({})
            continue
        kept = {b: w for b, w in row.items() if w >= threshold}
        if not kept:
            best = sorted(row.items(), key=lambda t: (-t[1], t[0]))[0]
            out.append({best[0]: 1.0})
        else:
            out.append(_renorm(kept))
    return out


def smooth_rows(rows, offsets, neighbors, passes_=10):
    """Fixed-count neighbor averaging in weight space (renorm each pass)."""
    work = [dict(r) for r in rows]
    for _ in range(passes_):
        nxt = []
        for i, row in enumerate(work):
            nbs = neighbors[offsets[i] : offsets[i + 1]]
            if not row or len(nbs) == 0:
                nxt.append(dict(row))
                continue
            acc = {}
            for nb in nbs:
                for b, w in work[int(nb)].items():
                    acc[b] = acc.get(b, 0.0) + w
            for b in acc:
                acc[b] /= len(nbs)
            merged = {}
            for b in sorted(set(row) | set(acc)):
                merged[b] = (1.0 - CLEANUP_SMOOTH_BLEND) * row.get(
                    b, 0.0
                ) + CLEANUP_SMOOTH_BLEND * acc.get(b, 0.0)
            nxt.append(_renorm(merged))
        work = nxt
    return work


def mirror_rows(rows, mesh_obj):
    """Mirror weights across X: nearest partner to the mirrored position,
    bone names L<->R swapped. Reads the original rows for every vert."""
    n = len(mesh_obj.data.vertices)
    tree = KDTree(n)
    for v in mesh_obj.data.vertices:
        tree.insert(v.co, v.index)
    tree.balance()
    out = []
    for v in mesh_obj.data.vertices:
        mirrored = (-v.co.x, v.co.y, v.co.z)
        _co, partner, _dist = tree.find(mirrored)
        mapped = {}
        for bone, w in rows[partner].items():
            mapped[mirror_bone(bone)] = mapped.get(mirror_bone(bone), 0.0) + w
        out.append(mapped)
    return out
