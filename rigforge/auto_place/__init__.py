# SPDX-FileCopyrightText: 2026 rigforge contributors
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""Deterministic metarig auto-placement (detector + fitter + gate).

Single home of the auto-placement implementation (docs/auto_placement.md):
`wm.rigforge_auto_place` and the headless `tools/` CLIs (thin shims that
re-export these modules) share this code, so UI and headless fits cannot
drift apart.
"""
