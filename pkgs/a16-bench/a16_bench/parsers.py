"""Pure parsers for benchmark tool output.

Each function takes the tool's raw output (text or parsed JSON) and returns
plain dicts. No I/O here, so tests/ can feed recorded fixtures.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# --- glmark2 / vkmark ----------------------------------------------------

# Both print one line per scene, e.g.
#   [build] use-vbo=false: FPS: 2543 FrameTime: 0.393 ms
#   [shading] shading=phong:duration=1: FPS: 63594 FrameTime: 0.016 ms
#   [desktop] <default>: Unsupported
_SCENE_RE = re.compile(
    r"^\[(?P<scene>[^\]]+)\]\s+(?P<opts>.*?):\s+FPS:\s+(?P<fps>\d+)\s+FrameTime:\s+(?P<ft>[\d.]+)\s*ms"
)
_SCENE_BAD_RE = re.compile(r"^\[(?P<scene>[^\]]+)\]\s+(?P<opts>.*?):\s+(?P<what>Unsupported|Failed.*|Set up failed.*)$")
_SCORE_RE = re.compile(r"(?:glmark2|vkmark) Score:\s+(\d+)")


def parse_mark(text: str) -> dict[str, Any]:
    """glmark2 or vkmark output -> {score, scenes: [{scene, options, fps, frametime_ms}], failed: [...], info}."""
    scenes: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    score = None
    info: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        m = _SCENE_RE.match(line)
        if m:
            scenes.append(
                {
                    "scene": m["scene"],
                    "options": m["opts"],
                    "fps": int(m["fps"]),
                    "frametime_ms": float(m["ft"]),
                }
            )
            continue
        m = _SCENE_BAD_RE.match(line)
        if m:
            failed.append({"scene": m["scene"], "options": m["opts"], "error": m["what"]})
            continue
        m = _SCORE_RE.search(line)
        if m:
            score = int(m[1])
            continue
        # "GL_RENDERER:    ...", "Device Name:    ..."
        if ":" in line and not line.startswith("["):
            key, _, val = line.partition(":")
            key = key.strip()
            if key in ("GL_VENDOR", "GL_RENDERER", "GL_VERSION", "Surface Size", "Device Name", "Driver Version", "Vendor ID", "Device ID"):
                info[key] = val.strip()
    return {"score": score, "scenes": scenes, "failed": failed, "info": info}


def scene_key(scene: dict[str, Any]) -> str:
    """Stable metric key for a scene: 'build[use-vbo=false]'."""
    opts = scene["options"]
    if opts in ("", "<default>"):
        return scene["scene"]
    return f"{scene['scene']}[{opts}]"


def is_software_renderer(info: dict[str, str]) -> bool:
    name = (info.get("GL_RENDERER", "") + " " + info.get("Device Name", "")).lower()
    return any(s in name for s in ("llvmpipe", "softpipe", "lavapipe", "swrast"))


# --- 7-Zip benchmark -----------------------------------------------------


def parse_7zip_bench(text: str) -> dict[str, float]:
    """`7zz b` output -> compress/decompress/total ratings (MIPS) and usage.

    Avr:      7228    99   7508   7463  |     127125   100  11112  11080
    Tot:             100   9310   9272
    """
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("Avr:"):
            left, _, right = line[4:].partition("|")
            lf, rf = left.split(), right.split()
            if len(lf) >= 4:
                out["compress_kib_s"] = float(lf[0])
                out["compress_usage_pct"] = float(lf[1])
                out["compress_rating_mips"] = float(lf[3])
            if len(rf) >= 4:
                out["decompress_kib_s"] = float(rf[0])
                out["decompress_usage_pct"] = float(rf[1])
                out["decompress_rating_mips"] = float(rf[3])
        elif line.startswith("Tot:"):
            f = line[4:].split()
            if len(f) >= 3:
                out["total_usage_pct"] = float(f[0])
                out["total_ru_mips"] = float(f[1])
                out["total_rating_mips"] = float(f[2])
    if "total_rating_mips" not in out:
        raise ValueError("no 'Tot:' line in 7-Zip benchmark output")
    return out


# --- stress-ng -----------------------------------------------------------


def parse_stressng_yaml(text: str) -> dict[str, dict[str, float]]:
    """stress-ng --yaml file -> {stressor: {bogo_ops, bogo_ops_s_real, bogo_ops_s_cpu, wall_s}}."""
    doc = yaml.safe_load(text) or {}
    out: dict[str, dict[str, float]] = {}
    for m in doc.get("metrics") or []:
        name = m.get("stressor")
        if not name:
            continue
        out[name] = {
            "bogo_ops": float(m.get("bogo-ops", 0)),
            "bogo_ops_s_real": float(m.get("bogo-ops-per-second-real-time", 0)),
            "bogo_ops_s_cpu": float(m.get("bogo-ops-per-second-usr-sys-time", 0)),
            "wall_s": float(m.get("wall-clock-time", 0)),
        }
    if not out:
        raise ValueError("no metrics in stress-ng YAML")
    return out


# --- sysbench memory -----------------------------------------------------

_SYSBENCH_MEM_RE = re.compile(r"MiB transferred \((?P<rate>[\d.]+) MiB/sec\)")


def parse_sysbench_memory(text: str) -> dict[str, float]:
    m = _SYSBENCH_MEM_RE.search(text)
    if not m:
        raise ValueError("no 'MiB transferred' line in sysbench output")
    out = {"mib_s": float(m["rate"])}
    t = re.search(r"total time:\s+([\d.]+)s", text)
    if t:
        out["total_time_s"] = float(t[1])
    return out


# --- tinymembench --------------------------------------------------------

_TMB_BW_RE = re.compile(r"^\s(?P<name>\S.*?)\s*:\s+(?P<mbs>[\d.]+) MB/s")
_TMB_LAT_RE = re.compile(r"^\s*(?P<size>\d+)\s*:\s*(?P<single>[\d.]+) ns\s*/\s*(?P<dual>[\d.]+) ns")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def parse_tinymembench(text: str) -> dict[str, Any]:
    """-> {bandwidth_mb_s: {slug: MB/s}, latency_ns: {"nohuge"|"huge": {size: {single, dual}}}}."""
    bw: dict[str, float] = {}
    lat: dict[str, dict[int, dict[str, float]]] = {}
    mode = None
    for line in text.splitlines():
        if line.startswith("block size"):
            mode = "huge" if "[MADV_HUGEPAGE]" in line else "nohuge"
            lat.setdefault(mode, {})
            continue
        if mode is not None:
            m = _TMB_LAT_RE.match(line)
            if m:
                lat[mode][int(m["size"])] = {"single": float(m["single"]), "dual": float(m["dual"])}
            continue
        m = _TMB_BW_RE.match(line)
        if m:
            bw[_slug(m["name"])] = float(m["mbs"])
    if not bw:
        raise ValueError("no bandwidth lines in tinymembench output")
    return {"bandwidth_mb_s": bw, "latency_ns": lat}


# --- fio -----------------------------------------------------------------


def parse_fio(doc: dict[str, Any]) -> dict[str, dict[str, float]]:
    """fio --output-format=json -> {jobname: {read_mb_s, read_iops, read_lat_us, read_p99_us, write_...}}."""
    out: dict[str, dict[str, float]] = {}
    for job in doc.get("jobs", []):
        res: dict[str, float] = {}
        for d in ("read", "write"):
            s = job.get(d) or {}
            if not s.get("io_bytes"):
                continue
            res[f"{d}_mb_s"] = s["bw"] * 1024 / 1e6  # fio bw is KiB/s
            res[f"{d}_iops"] = float(s["iops"])
            clat = s.get("clat_ns") or {}
            if clat.get("mean") is not None:
                res[f"{d}_lat_us"] = clat["mean"] / 1000
            pct = clat.get("percentile") or {}
            p99 = pct.get("99.000000")
            if p99 is not None:
                res[f"{d}_p99_us"] = p99 / 1000
        if job.get("error"):
            res["error"] = float(job["error"])
        out[job["jobname"]] = res
    if not out:
        raise ValueError("no jobs in fio JSON")
    return out


# --- iperf3 --------------------------------------------------------------


def parse_iperf3(doc: dict[str, Any]) -> dict[str, float]:
    """iperf3 -J -> receiver-side rate (Mbit/s) and sender retransmits."""
    if doc.get("error"):
        raise ValueError(f"iperf3: {doc['error']}")
    end = doc.get("end") or {}
    recv = end.get("sum_received") or {}
    sent = end.get("sum_sent") or {}
    if "bits_per_second" not in recv:
        raise ValueError("iperf3 JSON has no end.sum_received")
    out = {"mbit_s": recv["bits_per_second"] / 1e6}
    if "retransmits" in sent:
        out["retransmits"] = float(sent["retransmits"])
    return out


# --- systemd-analyze -----------------------------------------------------

_SA_PART_RE = re.compile(r"(?P<t>(?:\d+h\s*)?(?:\d+min\s*)?[\d.]+m?s)\s+\((?P<name>\w+)\)")


def parse_duration(s: str) -> float:
    """systemd timespan ('2min 2.451s', '600ms', '1h 2min 3s', '15.576s') -> seconds."""
    total = 0.0
    found = False
    for num, unit in re.findall(r"([\d.]+)\s*(h|min|ms|us|s)", s):
        found = True
        total += float(num) * {"h": 3600, "min": 60, "s": 1, "ms": 1e-3, "us": 1e-6}[unit]
    if not found:
        raise ValueError(f"not a systemd timespan: {s!r}")
    return total


def parse_systemd_analyze_time(text: str) -> dict[str, float]:
    """`systemd-analyze time` -> {firmware_s, loader_s, kernel_s, initrd_s, userspace_s, total_s, graphical_s}."""
    out: dict[str, float] = {}
    first = text.strip().splitlines()[0] if text.strip() else ""
    if not first.startswith("Startup finished in"):
        raise ValueError(f"boot not finished or unexpected output: {first!r}")
    body, _, total = first.partition("=")
    for m in _SA_PART_RE.finditer(body):
        out[f"{m['name']}_s"] = parse_duration(m["t"])
    if total.strip():
        out["total_s"] = parse_duration(total)
    g = re.search(r"graphical\.target reached after (.+?) in userspace", text)
    if g:
        out["graphical_target_s"] = parse_duration(g[1])
    return out


# --- qcom_stats / sysfs key-value files ----------------------------------


def parse_qcom_stat(text: str) -> dict[str, int]:
    """One /sys/kernel/debug/qcom_stats/<name> file -> {count, accumulated_ticks}.

    Count: 12
    Last Entered At: 123
    Last Exited At: 456
    Accumulated Duration: 789
    """
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if not val:
            continue
        try:
            n = int(val.split()[0], 0)
        except ValueError:
            continue
        if key == "count":
            out["count"] = n
        elif key.startswith("accumulated"):
            out["accumulated_ticks"] = n
    return out


def parse_uevent(text: str) -> dict[str, str]:
    """power_supply uevent (POWER_SUPPLY_FOO=bar) -> {foo: bar}."""
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.removeprefix("POWER_SUPPLY_").lower()] = v
    return out


def parse_suspend_stats(files: dict[str, str]) -> dict[str, int]:
    out = {}
    for k, v in files.items():
        try:
            out[k] = int(v.strip().split()[0])
        except (ValueError, IndexError):
            pass
    return out
