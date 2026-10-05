# SPDX-License-Identifier: GPL-2.0-or-later
"""ML joint hints as SOFT priors for the metarig fitter (Phase 4.5).

Reads a skintokens-joints/1 document (`skintokens joints`, the current
source, ~/SWE/blender/skintokens) or a legacy unirig-joints/1 one (same
schema; unirig-mac is retired). Coordinates only — see
docs/auto_placement.md Section 4.5 for the license boundary. Each hint is
arbitrated against the measured landmarks:

- hint agrees with the measured landmark (within AGREE_TOL) -> trust
  the hint (its position becomes the fitter target);
- hint disagrees -> the measurement wins, and the divergence is
  reported with both positions and the delta.

Two naming paths feed the same arbitration:

1. Semantic names (mixamo / vroid vocabularies) map onto quadruped
   landmark roles via UNIRIG_TO_ROLE (~20 entries). A biped vocabulary
   on a quadruped is approximate by nature: arms read as front legs,
   legs as rear legs; Hips/Spine2/Neck/Head as the spine chain. Names
   with no correspondent (shoulders, mid-spine segments) are reported
   unmapped, never guessed.
2. Generic bone_N names (the articulationxl creature class emits these;
   the stalker seed42 sample has 10) take an auto-role: the nearest
   measured landmark point. A hint far from every landmark still
   arbitrates — it just diverges loudly.

Frame: hint `normalized` coords map through the SUBJECT normalizer
(same formula as the exporter: longest bbox extent -> [-1, 1]), so the
mapping survives scale differences between the hint source mesh and
the fitting subject. When auto_rig normalized a Y-up subject into Z-up
(rotated=True), the same +90 deg X rotation applies to the hint coords
first. Confidence is null in both formats (uniform weighting).

Pure Python (no bpy): runnable with system python for tests, imported
by tools/fit_metarig.py for the --hints path.
"""

import json
import math
import sys

FORMAT = "skintokens-joints/1"
FORMATS = (FORMAT, "unirig-joints/1")  # same schema; unirig-joints/1 is legacy

# Agree tolerance: the longitudinal slice pitch is ~2 cm (length/120)
# plus envelope smoothing, so the same landmark remeasured lands
# within ~2 cm; 0.05 m is the same-point band with headroom. A
# 0.15 m band trusted a 12 cm-off spine hint (mid-back vs withers) and
# broke a passing fit (neck.001 margin 6.0 mm) — observed, then
# tightened. Semantic mismatches (0.2-0.7 m) diverge either way.
AGREE_TOL = 0.05

# Landmark roles address measured points: midline names plus
# {FL,FR,BL,BR}.{top,mid,foot,toe}. Only .L-side roles apply to fitter
# targets (.R mirrors .L by construction); .R agreements are supporting
# evidence, reported but not applied.
_MIXAMO_SIDE = {
    "LeftArm": "FL.top",
    "LeftForeArm": "FL.mid",
    "LeftHand": "FL.foot",
    "LeftUpLeg": "BL.top",
    "LeftLeg": "BL.mid",
    "LeftFoot": "BL.foot",
    "LeftToeBase": "BL.toe",
    "RightArm": "FR.top",
    "RightForeArm": "FR.mid",
    "RightHand": "FR.foot",
    "RightUpLeg": "BR.top",
    "RightLeg": "BR.mid",
    "RightFoot": "BR.foot",
    "RightToeBase": "BR.toe",
}
_VROID_SIDE = {
    "J_Bip_L_UpperArm": "FL.top",
    "J_Bip_L_LowerArm": "FL.mid",
    "J_Bip_L_Hand": "FL.foot",
    "J_Bip_L_UpperLeg": "BL.top",
    "J_Bip_L_LowerLeg": "BL.mid",
    "J_Bip_L_Foot": "BL.foot",
    "J_Bip_L_ToeBase": "BL.toe",
    "J_Bip_R_UpperArm": "FR.top",
    "J_Bip_R_LowerArm": "FR.mid",
    "J_Bip_R_Hand": "FR.foot",
    "J_Bip_R_UpperLeg": "BR.top",
    "J_Bip_R_LowerLeg": "BR.mid",
    "J_Bip_R_Foot": "BR.foot",
    "J_Bip_R_ToeBase": "BR.toe",
}
UNIRIG_TO_ROLE = {
    "mixamorig:Hips": "spine_rear",
    "mixamorig:Neck": "neck_base",
    "mixamorig:Head": "head",
    **{f"mixamorig:{k}": v for k, v in _MIXAMO_SIDE.items()},
    "J_Bip_C_Hips": "spine_rear",
    "J_Bip_C_Neck": "neck_base",
    "J_Bip_C_Head": "head",
    **_VROID_SIDE,
}
# Deliberately unmapped (no quadruped correspondent, never guessed):
# Shoulders, Spine/Spine1/Spine2 (mid-spine), hand/finger chains.

# Landmark address -> stalker fitter target key (L side only; R mirrors).
ROLE_TO_TARGET = {
    "spine_front": "spine_front",
    "spine_rear": "spine_rear",
    "neck_base": "neck_base",
    "head": "head",
    "skull": "skull",
    "tail_base": "tail_base",
    "FL.top": "front_top",
    "FL.mid": "front_mid",
    "FL.foot": "front_foot",
    "FL.toe": "front_toe",
    "BL.top": "rear_top",
    "BL.mid": "rear_mid",
    "BL.foot": "rear_foot",
    "BL.toe": "rear_toe",
}


def landmark_points(lm):
    """Flatten a quadruped landmark dict to {role: [x, y, z]}."""
    pts = {}
    for key in ("spine_front", "spine_rear", "neck_base", "head", "skull", "tail_base"):
        if lm.get(key) is not None:
            pts[key] = list(lm[key])
    for leg, joints in (lm.get("legs") or {}).items():
        for joint in ("top", "mid", "foot", "toe"):
            if joints.get(joint) is not None:
                pts[f"{leg}.{joint}"] = list(joints[joint])
    return pts


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def to_fit_frame(normalized, center, scale, rotated):
    """Hint normalized coords -> fitting-frame position."""
    x, y, z = normalized
    if rotated:  # same +90 deg X rotation auto_rig applies to Y-up subjects
        y, z = -z, y
    return [center[0] + x * scale, center[1] + y * scale, center[2] + z * scale]


def subject_normalizer(bounds):
    """Center/scale over a bounds dict (exporter formula)."""
    lo = [bounds["xmin"], bounds["ymin"], bounds["zmin"]]
    hi = [bounds["xmax"], bounds["ymax"], bounds["zmax"]]
    center = [(a + b) / 2 for a, b in zip(lo, hi, strict=True)]
    return center, max(b - a for a, b in zip(lo, hi, strict=True)) / 2


def load_hints(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("format") not in FORMATS:
        raise ValueError(
            f"{path}: format {doc.get('format')!r}, need one of {FORMATS!r}"
        )
    if not doc.get("joints"):
        raise ValueError(f"{path}: no joints")
    for j in doc["joints"]:
        if j.get("normalized") is None or j.get("name") is None:
            raise ValueError(f"{path}: joint missing name/normalized: {j}")
    return doc


def arbitrate(doc, lm, rotated=False, tol=AGREE_TOL):
    """Agree-vs-measure arbitration; returns the hints report dict."""
    pts = landmark_points(lm)
    if "bounds" not in lm:
        raise ValueError("landmarks need bounds for the hint frame mapping")
    center, scale = subject_normalizer(lm["bounds"])
    trusted, supporting, agreed, diverged, unmapped = {}, {}, [], [], []
    for joint in doc["joints"]:
        name = joint["name"]
        pos = to_fit_frame(joint["normalized"], center, scale, rotated)
        role = UNIRIG_TO_ROLE.get(name)
        auto = False
        if role is None:  # bone_N path: nearest measured landmark
            role = min(pts, key=lambda r: dist(pos, pts[r]))
            auto = True
        if role not in pts:
            unmapped.append({"hint": name, "role": role, "reason": "no such landmark"})
            continue
        delta = dist(pos, pts[role])
        entry = {
            "hint": name,
            "role": role,
            "auto_role": auto,
            "hint_pos": [round(v, 4) for v in pos],
            "measured_pos": [round(v, 4) for v in pts[role]],
            "delta": round(delta, 4),
        }
        if delta <= tol:
            entry["verdict"] = "trust-hint"
            agreed.append(entry)
            target = ROLE_TO_TARGET.get(role)
            if target is not None:
                trusted[target] = pos
            else:
                supporting[role] = pos  # R side: confirms, R mirrors L
        else:
            entry["verdict"] = "measure-wins"
            diverged.append(entry)
    return {
        "source": doc.get("source"),
        "subject": doc.get("subject"),
        "seed": doc.get("seed"),
        "joints": len(doc["joints"]),
        "tol": tol,
        "rotated": rotated,
        "trusted": {k: [round(v, 4) for v in p] for k, p in trusted.items()},
        "supporting": {k: [round(v, 4) for v in p] for k, p in supporting.items()},
        "agreed": agreed,
        "diverged": diverged,
        "unmapped": unmapped,
    }


def main():
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    if len(args) < 2:
        print("usage: unirig_hints.py HINTS.json LANDMARKS.json [--rotated]")
        raise SystemExit(2)
    doc = load_hints(args[0])
    with open(args[1], encoding="utf-8") as f:
        lm = json.load(f)
    report = arbitrate(doc, lm, rotated="--rotated" in args)
    print(f"hints: {len(report['agreed'])} agree, {len(report['diverged'])} diverge")
    for e in report["agreed"]:
        print(f"  AGREE {e['hint']} -> {e['role']} delta={e['delta']}")
    for e in report["diverged"]:
        print(f"  DIVERGE {e['hint']} -> {e['role']} delta={e['delta']}")
    print("RIGFORGE_HINTS_BEGIN")
    print(json.dumps(report, indent=2))
    print("RIGFORGE_HINTS_OK")


if __name__ == "__main__":
    main()
