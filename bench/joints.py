#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""rigforge auto-rig benchmark on CC0 people. One command, headless:

    python3 bench/joints.py [--tag NAME] [--only a,b]

Inputs: the shared CC0 corpus (8 MPFB2 characters in clothes, varied body
types, 3k-21k verts; ~/Games/_blender/weightforge/bench/corpus/fetch.sh puts
them in $FORGE_BENCH/corpus). Each comes with MPFB2's own skeleton, fitted
to that body by MakeHuman: the ground truth for where joints belong.

Per character: strip the rig, run tools/auto_rig.py (hll_hero), and
measure joint placement against the truth (hips, neck, head, shoulders,
upper arms, elbows, wrists, hips joints, knees, ankles, toes), in % of the
character's height; also wall time, success, and rfcheck on the GLB when
the binary is built (~/Games/_blender/rfcheck/target/release/rfcheck). Free baseline:
Blender's stock Rigify human metarig scaled to the character's height (what
you start from before placing bones by hand). Writes
bench/results/<tag>/{summary.json,scorecard.md} and copies both to
bench/history/ (tracked).
"""

import argparse
import datetime
import json
import math
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FORGE = os.environ.get("FORGE_BENCH", os.path.expanduser("~/.cache/forge-bench"))
CORPUS = os.path.join(FORGE, "corpus")
RFCHECK = os.environ.get("RFCHECK", os.path.expanduser("~/Games/_blender/rfcheck/target/release/rfcheck"))

# rigforge / Rigify bone head -> MPFB2 game_engine bone head (same joint).
TRUTH = {"DEF-spine": "pelvis", "DEF-spine.004": "neck_01", "DEF-spine.006": "head",
         "DEF-shoulder.L": "clavicle_l", "DEF-upper_arm.L": "upperarm_l", "DEF-forearm.L": "lowerarm_l",
         "DEF-hand.L": "hand_l", "DEF-thigh.L": "thigh_l", "DEF-shin.L": "calf_l", "DEF-foot.L": "foot_l",
         "DEF-toe.L": "ball_l"}
for k, v in list(TRUTH.items()):
    if k.endswith(".L"):
        TRUTH[k[:-2] + ".R"] = v[:-2] + "_r"
GROUPS = {"spine/head": ["DEF-spine", "DEF-spine.004", "DEF-spine.006"],
          "arms": ["DEF-shoulder", "DEF-upper_arm", "DEF-forearm", "DEF-hand"],
          "legs": ["DEF-thigh", "DEF-shin", "DEF-foot", "DEF-toe"]}


def blender_bin():
    for c in (os.environ.get("BLENDER_BIN"), os.environ.get("BLENDER"), shutil.which("blender"),
              "/Applications/Blender.app/Contents/MacOS/Blender"):
        if c and os.path.exists(c):
            return os.path.realpath(c)
    sys.exit("set $BLENDER_BIN")


def bl(blender, *args, env=None):
    t0 = time.time()
    p = subprocess.run([blender, "-b", "--factory-startup", "--python", *args], capture_output=True, text=True, env=env)
    return p, round(time.time() - t0, 2)


def errors(pred, truth, height):
    out = {}
    for k, t in TRUTH.items():
        if k in pred and t in truth:
            out[k] = math.dist(pred[k], truth[t]) / height * 100
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=datetime.date.today().isoformat())
    ap.add_argument("--only")
    a = ap.parse_args()
    blender = blender_bin()
    scripts = os.path.join("/tmp", "rf_bench_scripts")
    shutil.rmtree(scripts, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "rigforge"), os.path.join(scripts, "addons", "rigforge"))
    env = dict(os.environ, BLENDER_USER_SCRIPTS=scripts)
    out = os.path.join(HERE, "results", a.tag)
    os.makedirs(out, exist_ok=True)
    names = sorted(os.listdir(CORPUS))
    if a.only:
        names = [n for n in names if n in a.only.split(",")]
    summary = {"tag": a.tag, "date": datetime.datetime.now().isoformat(timespec="seconds"),
               "commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                        text=True).stdout.strip(), "characters": {}}
    rb = os.path.join(HERE, "rig_blender.py")
    for n in names:
        w = os.path.join(out, n)
        os.makedirs(w, exist_ok=True)
        mesh, truth_p = os.path.join(w, "mesh.glb"), os.path.join(w, "truth.json")
        bl(blender, rb, "--", "strip", os.path.join(CORPUS, n, "ref.glb"), mesh, truth_p)
        truth = json.load(open(truth_p))
        zs = [v[2] for v in truth.values()]
        meta = json.load(open(os.path.join(CORPUS, n, "meta.json")))
        rigged = os.path.join(w, "rigged.glb")
        p, dt = bl(blender, os.path.join(ROOT, "tools", "auto_rig.py"), "--", mesh, "--preset", "hll_hero",
                   "--out", rigged, env=env)
        verdict = (p.stdout.strip().splitlines() or [""])[-1]
        row = {"verts": meta["verts"], "seconds": dt, "ok": os.path.exists(rigged) and "OK" in verdict,
               "verdict": verdict[:300]}
        # height from the clean mesh is in the strip step; use the truth's head tail as the top
        height = max(zs) - min(zs)
        row["height_m"] = height
        if row["ok"]:
            jp = os.path.join(w, "rigged_joints.json")
            bl(blender, rb, "--", "joints", rigged, jp)
            row["rigforge"] = errors(json.load(open(jp)), truth, height)
            if os.path.exists(RFCHECK):
                r = subprocess.run([RFCHECK, "--class", "hero", rigged], capture_output=True, text=True)
                row["rfcheck_exit"] = r.returncode
                row["rfcheck"] = r.stdout.strip().splitlines()[:6]
        mp = os.path.join(w, "metarig.json")
        bl(blender, rb, "--", "metarig", mesh, mp)
        if os.path.exists(mp):
            row["metarig"] = errors(json.load(open(mp)), truth, height)
        summary["characters"][n] = row
        mean = lambda d: sum(d.values()) / len(d) if d else float("nan")
        print(f"{n}: ok={row['ok']} {dt}s rigforge {mean(row.get('rigforge', {})):.2f}% "
              f"metarig {mean(row.get('metarig', {})):.2f}%  {verdict[:120]}", flush=True)
        with open(os.path.join(out, "summary.json"), "w") as fh:
            json.dump(summary, fh, indent=1)
    write_scorecard(summary, os.path.join(out, "scorecard.md"))
    hist = os.path.join(HERE, "history")
    os.makedirs(hist, exist_ok=True)
    shutil.copy(os.path.join(out, "summary.json"), os.path.join(hist, f"{a.tag}.json"))
    shutil.copy(os.path.join(out, "scorecard.md"), os.path.join(hist, f"{a.tag}.md"))
    print(open(os.path.join(out, "scorecard.md")).read())


def write_scorecard(s, path):
    C = s["characters"]

    def grp(method, g):
        xs = [v for c in C.values() for k, v in c.get(method, {}).items()
              if any(k == b or k.startswith(b + ".") for b in GROUPS[g])]
        return sum(xs) / len(xs) if xs else float("nan")

    def allm(method):
        xs = [v for c in C.values() for v in c.get(method, {}).values()]
        return (sum(xs) / len(xs), max(xs)) if xs else (float("nan"), float("nan"))
    ok = sum(1 for c in C.values() if c["ok"])
    L = [f"# rigforge auto-rig bench `{s['tag']}` ({s['commit']}, {s['date']})", "",
         f"{len(C)} CC0 MPFB2 characters; joint placement error vs MPFB2's fitted skeleton, % of height "
         f"(1% ~ 1.8 cm on an adult). rigforge succeeded on {ok}/{len(C)}.", "",
         "| method | mean | worst | spine/head | arms | legs |", "|---|---|---|---|---|---|"]
    for m, label in (("rigforge", "rigforge auto_rig (hll_hero)"), ("metarig", "Rigify metarig, scaled (free baseline)")):
        mn, mx = allm(m)
        L.append(f"| {label} | {mn:.2f} | {mx:.2f} | {grp(m, 'spine/head'):.2f} | {grp(m, 'arms'):.2f} | {grp(m, 'legs'):.2f} |")
    L += ["", "| character | verts | ok | seconds | rigforge mean % | metarig mean % | verdict |", "|---|---|---|---|---|---|---|"]
    for n, c in C.items():
        mean = lambda d: f"{sum(d.values()) / len(d):.2f}" if d else "-"
        L.append(f"| {n} | {c['verts']} | {c['ok']} | {c['seconds']} | {mean(c.get('rigforge', {}))} | "
                 f"{mean(c.get('metarig', {}))} | {c['verdict'][:60]} |")
    with open(path, "w") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
