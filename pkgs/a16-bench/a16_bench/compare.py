"""Compare two runs: what changed in the setup, and metric deltas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(ref: str, root: Path) -> dict[str, Any]:
    """A run by results.json path, run directory, or run id under root
    (a unique prefix or suffix of the id also works, e.g. a label)."""
    p = Path(ref)
    if p.is_file():
        return json.loads(p.read_text())
    if p.is_dir() and (p / "results.json").is_file():
        return json.loads((p / "results.json").read_text())
    if (root / ref / "results.json").is_file():
        return json.loads((root / ref / "results.json").read_text())
    cands = sorted(d for d in root.glob("*") if (d / "results.json").is_file() and (d.name.startswith(ref) or d.name.endswith(ref)))
    if len(cands) == 1:
        return json.loads((cands[0] / "results.json").read_text())
    if len(cands) > 1:
        raise SystemExit(f"{ref!r} is ambiguous: {', '.join(c.name for c in cands)}")
    raise SystemExit(f"no run {ref!r} (looked in {root})")


def _get(d: dict[str, Any], path: str):
    for k in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


SETUP_KEYS = [
    ("kernel", "meta.kernel.release"),
    ("generation", "meta.system.booted_generation"),
    ("revision", "meta.nixos.configurationRevision"),
    ("nixos", "meta.nixos.nixosVersion"),
    ("system", "meta.system.booted_system"),
    ("power", "meta.power.source"),
    ("battery %", "meta.power.battery.capacity"),
    ("bios", "meta.dmi.bios_version"),
    ("bench", "meta.bench_version"),
    ("profile", "profile"),
    ("complete", "complete"),
]


def setup_diff(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, path in SETUP_KEYS:
        va, vb = _get(a, path), _get(b, path)
        if va != vb:
            out.append({"what": name, "a": va, "b": vb})
    ca = set((_get(a, "meta.cmdline") or "").split())
    cb = set((_get(b, "meta.cmdline") or "").split())
    if ca != cb:
        out.append({"what": "cmdline", "a": sorted(ca - cb), "b": sorted(cb - ca)})
    ta, tb = _get(a, "meta.tools") or {}, _get(b, "meta.tools") or {}
    for t in sorted(set(ta) | set(tb)):
        if ta.get(t) != tb.get(t):
            out.append({"what": f"tool {t}", "a": ta.get(t), "b": tb.get(t), "warn": "tool version differs: scores may not be comparable"})
    return out


def verdict(better: str | None, delta_pct: float | None, threshold: float) -> str:
    if better is None or delta_pct is None:
        return "info"
    if abs(delta_pct) < threshold:
        return "same"
    up = delta_pct > 0
    return "better" if up == (better == "higher") else "worse"


def compare(a: dict[str, Any], b: dict[str, Any], threshold: float = 5.0, everything: bool = False) -> dict[str, Any]:
    rows = []
    status = []
    ta, tb = a.get("tests", {}), b.get("tests", {})
    for test in list(ta) + [t for t in tb if t not in ta]:
        ra, rb = ta.get(test, {}), tb.get(test, {})
        sa, sb = ra.get("status"), rb.get("status")
        if sa != sb or sa != "ok":
            status.append({"test": test, "a": sa, "b": sb, "a_reason": ra.get("reason"), "b_reason": rb.get("reason")})
        ma, mb = ra.get("metrics") or {}, rb.get("metrics") or {}
        for key in list(ma) + [k for k in mb if k not in ma]:
            xa, xb = ma.get(key), mb.get(key)
            ref = xa or xb
            if not everything and not ref.get("headline"):
                continue
            va = xa["value"] if xa else None
            vb = xb["value"] if xb else None
            d = None
            if va is not None and vb is not None and va != 0:
                d = 100.0 * (vb - va) / abs(va)
            rows.append(
                {
                    "test": test,
                    "metric": key,
                    "unit": ref.get("unit", ""),
                    "a": va,
                    "b": vb,
                    "delta_pct": None if d is None else round(d, 2),
                    "verdict": verdict(ref.get("better"), d, threshold) if xa and xb else "missing",
                }
            )
    return {
        "a": a.get("run_id"),
        "b": b.get("run_id"),
        "threshold_pct": threshold,
        "setup": setup_diff(a, b),
        "status": status,
        "metrics": rows,
        "worse": sum(1 for r in rows if r["verdict"] == "worse"),
        "better": sum(1 for r in rows if r["verdict"] == "better"),
    }


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:.0f}" if abs(v) >= 1000 else f"{v:.4g}"
    return str(v)


def format_text(c: dict[str, Any]) -> str:
    lines = [f"A: {c['a']}", f"B: {c['b']}", ""]
    if c["setup"]:
        lines.append("Setup differences:")
        for d in c["setup"]:
            lines.append(f"  {d['what']:<12} A={d['a']}  B={d['b']}" + (f"  ({d['warn']})" if d.get("warn") else ""))
        lines.append("")
    if c["status"]:
        lines.append("Tests not ok in both:")
        for s in c["status"]:
            lines.append(f"  {s['test']:<14} A={s['a']} B={s['b']}" + (f"  [{s['b_reason'] or s['a_reason']}]" if (s["b_reason"] or s["a_reason"]) else ""))
        lines.append("")
    w = max([len(f"{r['test']} {r['metric']}") for r in c["metrics"]] + [10])
    lines.append(f"{'metric':<{w}}  {'A':>10}  {'B':>10}  {'unit':<10}  {'delta':>8}  verdict (threshold {c['threshold_pct']}%)")
    for r in c["metrics"]:
        d = "-" if r["delta_pct"] is None else f"{r['delta_pct']:+.1f}%"
        mark = {"worse": "WORSE", "better": "better", "same": "", "info": "(info)", "missing": "missing"}[r["verdict"]]
        lines.append(f"{r['test'] + ' ' + r['metric']:<{w}}  {_fmt(r['a']):>10}  {_fmt(r['b']):>10}  {r['unit']:<10}  {d:>8}  {mark}".rstrip())
    lines.append("")
    lines.append(f"{c['worse']} worse, {c['better']} better beyond {c['threshold_pct']}%")
    return "\n".join(lines)
