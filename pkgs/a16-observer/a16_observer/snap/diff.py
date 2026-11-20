"""Structured diff of two snapshot.json files.

Both are flattened to dotted paths ("usb.typec.c2.port.power_operation_mode");
the collector wrapper ("collectors.<topic>.<name>.data") is shortened to
"<topic>.<name>". Volatile fields (timestamps, elapsed times, PM residency
clocks, info-level red flags) are ignored unless --all."""

from __future__ import annotations

import re

VOLATILE = re.compile(
    r"(^|\.)(uptime_s|connected_duration|active_duration|runtime_active_time|runtime_suspended_time)$"
    # red flags: the per-pattern since_boot counts carry the news; known
    # noise (info), the window since another snapshot and the cursor don't
    r"|^redflags\.kernel\.(info\.|since_last|since_seq|top\.|by_severity\.info$)"
)
TOP_KEYS = ("host", "boot_id", "generation", "kernel", "port", "topic")


def flatten(obj, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = {}
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if not obj and prefix:
            out[prefix] = []
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


def snapshot_paths(snap: dict) -> dict:
    flat = {}
    for k in TOP_KEYS:
        if k in snap:
            flat[k] = snap[k]
    for name, res in (snap.get("collectors") or {}).items():
        if res.get("ok"):
            flatten(res.get("data"), name, flat)
        else:
            flat[f"{name}.error"] = res.get("error")
    for k in ("skipped", "errors"):
        flat[f"n_{k}"] = len(snap.get(k) or [])
    return flat


def diff(a: dict, b: dict, include_volatile: bool = False, grep: str | None = None) -> dict:
    fa, fb = snapshot_paths(a), snapshot_paths(b)
    rx = re.compile(grep) if grep else None

    def keep(p):
        if not include_volatile and VOLATILE.search(p):
            return False
        return not rx or bool(rx.search(p))

    changed, added, removed = {}, {}, {}
    for p in sorted(set(fa) | set(fb)):
        if not keep(p):
            continue
        if p in fa and p in fb:
            if fa[p] != fb[p]:
                changed[p] = [fa[p], fb[p]]
        elif p in fb:
            added[p] = fb[p]
        else:
            removed[p] = fa[p]
    return {
        "a": {k: a.get(k) for k in ("id", "label", "taken_at", "boot_id", "generation")},
        "b": {k: b.get(k) for k in ("id", "label", "taken_at", "boot_id", "generation")},
        "changed": changed,
        "added": added,
        "removed": removed,
    }


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        return int(v)
    return None


def _fmt(v) -> str:
    s = repr(v) if isinstance(v, str) else str(v)
    return s if len(s) <= 120 else s[:117] + "..."


def format_diff(d: dict) -> str:
    lines = [
        f"--- {d['a'].get('id')}  ({d['a'].get('label')}, gen {d['a'].get('generation')}, boot {str(d['a'].get('boot_id'))[:8]})",
        f"+++ {d['b'].get('id')}  ({d['b'].get('label')}, gen {d['b'].get('generation')}, boot {str(d['b'].get('boot_id'))[:8]})",
    ]
    if d["a"].get("boot_id") != d["b"].get("boot_id"):
        lines.append("note: different boots; cumulative counters restart at boot")
    for p, (x, y) in d["changed"].items():
        nx, ny = _num(x), _num(y)
        delta = f"  ({ny - nx:+d})" if isinstance(nx, int) and isinstance(ny, int) else ""
        lines.append(f"~ {p}: {_fmt(x)} -> {_fmt(y)}{delta}")
    for p, y in d["added"].items():
        lines.append(f"+ {p}: {_fmt(y)}")
    for p, x in d["removed"].items():
        lines.append(f"- {p}: {_fmt(x)}")
    n = len(d["changed"]) + len(d["added"]) + len(d["removed"])
    lines.append(f"{len(d['changed'])} changed, {len(d['added'])} added, {len(d['removed'])} removed" if n else "no differences")
    return "\n".join(lines)
