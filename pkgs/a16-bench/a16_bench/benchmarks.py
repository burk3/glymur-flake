"""The benchmarks. Each one checks its own preconditions (tools, power source,
root, hardware) and reports `skipped` with a reason instead of failing.

Metric keys are stable across runs; compare.py matches on (test, key).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import parsers
from .sampler import Channels, Sampler
from .sysfs import Probe

# --- results -------------------------------------------------------------


@dataclass
class Result:
    status: str = "ok"  # ok | skipped | failed | aborted
    reason: str | None = None
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    conditions: dict[str, Any] = field(default_factory=dict)
    raw: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)

    def metric(self, key: str, value: float | None, unit: str, better: str | None = "higher", headline: bool = False):
        if value is None:
            return
        self.metrics[key] = {"value": round(float(value), 4), "unit": unit, "better": better}
        if headline:
            self.metrics[key]["headline"] = True

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {}, [])} | {"status": self.status}


class Skip(Exception):
    pass


class Aborted(Exception):
    pass


# --- context ---------------------------------------------------------------


class Ctx:
    """What a benchmark gets: run dir, probe, options, and a command runner
    that records raw output and stops the workload at the abort temperature."""

    def __init__(self, run_dir: Path, probe: Probe, opts: dict[str, Any], log: Callable[[str], None] = print):
        self.run_dir = run_dir
        self.probe = probe
        self.opts = opts
        self.log = log
        self.channels = Channels(probe)
        self.sampler: Sampler | None = None
        self.inhibitor: Any = None  # runner's sleep inhibitor (s2idle pauses it)
        (run_dir / "raw").mkdir(parents=True, exist_ok=True)
        (run_dir / "traces").mkdir(parents=True, exist_ok=True)

    def run(self, name: str, cmd: list[str], timeout: float, result: Result | None = None, check: bool = True) -> str:
        """Run cmd, save its combined output to raw/<name>.txt, return stdout+stderr text."""
        rel = f"raw/{name}.txt"
        path = self.run_dir / rel
        self.log(f"  $ {shlex.join(cmd)}")
        with open(path, "w") as fh:
            fh.write(f"# {shlex.join(cmd)}\n")
            fh.flush()
            proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)
            deadline = time.monotonic() + timeout
            reason = None
            while proc.poll() is None:
                if self.sampler is not None and self.sampler.over_limit.is_set():
                    reason = f"die temperature {self.sampler.over_limit_c} C >= abort limit {self.sampler.abort_c} C"
                elif time.monotonic() > deadline:
                    reason = f"timeout after {timeout:.0f} s"
                if reason:
                    proc.terminate()
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    break
                time.sleep(0.25)
        if result is not None:
            result.raw.append(rel)
        text = path.read_text()
        body = text.split("\n", 1)[1] if "\n" in text else ""
        if reason:
            raise Aborted(f"{cmd[0]}: {reason}")
        if check and proc.returncode != 0:
            tail = " | ".join(body.strip().splitlines()[-3:])
            raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {tail}")
        return body


# --- benchmark definitions ---------------------------------------------------


@dataclass
class Bench:
    name: str
    title: str
    est_s: int
    tools: list[str]
    fn: Callable[[Ctx, Result, dict[str, Any]], None]
    params: dict[str, Any] = field(default_factory=dict)
    power: str = "any"  # any | battery | ac
    allcore: bool = False
    root: bool = False
    cooldown: bool = True
    sample: bool = True
    sample_interval: float = 1.0

    def precheck(self, opts: dict[str, Any], probe: Probe) -> str | None:
        """Reason to skip, or None. Cheap and side-effect free (also used by --dry-run)."""
        missing = [t for t in self.tools if not shutil.which(t)]
        if missing:
            return f"missing tools: {', '.join(missing)}"
        if self.allcore and not opts.get("allow_allcore"):
            return "all-core load is opt-in (--allow-allcore; silent resets under sustained load, bead baw)"
        if self.root and os.geteuid() != 0:
            return "needs root"
        if self.power != "any":
            src = probe.power_source()["source"]
            if src != self.power:
                return f"needs {self.power} power (now: {src})"
        return None


REGISTRY: dict[str, Bench] = {}


def bench(name: str, title: str, est_s: int, tools: list[str], **kw):
    def deco(fn):
        REGISTRY[name] = Bench(name, title, est_s, tools, fn, **kw)
        return fn

    return deco


# --- boot ----------------------------------------------------------------


@bench("boot", "Boot time (systemd-analyze)", 5, ["systemd-analyze"], cooldown=False, sample=False)
def b_boot(ctx: Ctx, r: Result, p: dict[str, Any]):
    out = ctx.run("boot-time", ["systemd-analyze", "time"], 30, r, check=False)
    try:
        t = parsers.parse_systemd_analyze_time(out)
    except ValueError as e:
        raise Skip(str(e))
    for k, v in t.items():
        r.metric(k, v, "s", "lower", headline=k in ("total_s", "kernel_s", "initrd_s", "userspace_s"))
    ctx.run("boot-blame", ["systemd-analyze", "blame", "--no-pager"], 30, r, check=False)
    ctx.run("boot-critical-chain", ["systemd-analyze", "critical-chain", "--no-pager"], 30, r, check=False)


# --- idle power ------------------------------------------------------------


@bench(
    "idle-power",
    "Idle power from the battery gauge (power_now average + energy_now delta)",
    90,
    [],
    params={"seconds": 60, "settle": 20},
    power="battery",
    sample_interval=2.0,
)
def b_idle_power(ctx: Ctx, r: Result, p: dict[str, Any]):
    bat = ctx.probe.battery()
    if not bat:
        raise Skip("no battery power_supply")
    r.conditions["display"] = ctx.probe.display()
    ctx.log(f"  settling {p['settle']} s, then measuring {p['seconds']} s (keep hands off)")
    time.sleep(p["settle"])
    e0 = _read_num(bat[1] / "energy_now")
    t0 = time.monotonic()
    rows0 = len(ctx.sampler.rows) if ctx.sampler else 0
    time.sleep(p["seconds"])
    e1 = _read_num(bat[1] / "energy_now")
    dt = time.monotonic() - t0
    rows = ctx.sampler.rows[rows0:] if ctx.sampler else []
    pw = [abs(x["bat.power_mw"]) for x in rows if x.get("bat.power_mw") is not None and x.get("bat.discharging") == 1.0]
    if not pw or len(pw) < len(rows):
        raise Skip("battery stopped discharging during the measurement (AC plugged in?)")
    r.metric("power_mw", sum(pw) / len(pw), "mW", "lower", headline=True)
    r.metric("power_mw_max", max(pw), "mW", "lower")
    if e0 is not None and e1 is not None and e0 != e1:
        # energy_now moves in ~12 mWh steps on the A16: only a coarse cross-check.
        r.metric("energy_delta_mw", (e0 - e1) / 1000 / (dt / 3600), "mW", None)
        r.conditions["energy_step_note"] = "energy_now has ~12 mWh resolution; trust power_mw"
    r.conditions["seconds"] = round(dt, 1)


def _read_num(path: Path) -> float | None:
    try:
        return float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


# --- CPU -----------------------------------------------------------------


def _policy_cpus(ctx: Ctx) -> list[tuple[str, int, float]]:
    """(policy, first cpu, max MHz) for each cpufreq policy; one CPU per cluster."""
    out = []
    for pol in ctx.probe.cpu_policies():
        if pol.cpus:
            out.append((pol.name, pol.cpus[0], (pol.max_khz or 0) / 1000))
    if not out:
        out = [("cpu0", 0, 0.0)]
    return out


@bench(
    "cpu-single",
    "Single-thread CPU per cluster: 7-Zip benchmark (-mmt1) and stress-ng cpu, pinned with taskset",
    180,
    ["7zz", "stress-ng", "taskset"],
    params={"passes": 2, "stressng_s": 15},
)
def b_cpu_single(ctx: Ctx, r: Result, p: dict[str, Any]):
    for pol, cpu, mhz in _policy_cpus(ctx):
        r.conditions.setdefault("cpus", {})[pol] = {"cpu": cpu, "max_mhz": mhz}
        out = ctx.run(f"7zz-{pol}", ["taskset", "-c", str(cpu), "7zz", "b", str(p["passes"]), "-mmt1"], 600, r)
        z = parsers.parse_7zip_bench(out)
        r.metric(f"{pol}.7zip_rating_mips", z["total_rating_mips"], "MIPS", headline=True)
        r.metric(f"{pol}.7zip_compress_mips", z.get("compress_rating_mips"), "MIPS")
        r.metric(f"{pol}.7zip_decompress_mips", z.get("decompress_rating_mips"), "MIPS")
        y = ctx.run_dir / "raw" / f"stressng-{pol}.yaml"
        ctx.run(
            f"stressng-{pol}",
            ["taskset", "-c", str(cpu), "stress-ng", "--cpu", "1", "--cpu-method", "all", "-t", str(p["stressng_s"]), "--metrics", "--yaml", str(y)],
            p["stressng_s"] + 60,
            r,
        )
        s = parsers.parse_stressng_yaml(y.read_text())["cpu"]
        r.metric(f"{pol}.stressng_cpu_bogo_ops_s", s["bogo_ops_s_real"], "bogo-ops/s", headline=True)


@bench(
    "cpu-multi",
    "All-core CPU: 7-Zip benchmark (all threads) and stress-ng cpu on every CPU, short",
    90,
    ["7zz", "stress-ng"],
    params={"passes": 1, "stressng_s": 30},
    allcore=True,
)
def b_cpu_multi(ctx: Ctx, r: Result, p: dict[str, Any]):
    n = os.cpu_count() or 1
    out = ctx.run("7zz-all", ["7zz", "b", str(p["passes"]), f"-mmt{n}"], 300, r)
    z = parsers.parse_7zip_bench(out)
    r.metric("7zip_rating_mips", z["total_rating_mips"], "MIPS", headline=True)
    r.metric("7zip_usage_pct", z.get("total_usage_pct"), "%", None)
    y = ctx.run_dir / "raw" / "stressng-all.yaml"
    ctx.run(
        "stressng-all",
        ["stress-ng", "--cpu", "0", "--cpu-method", "all", "-t", str(p["stressng_s"]), "--metrics", "--yaml", str(y)],
        p["stressng_s"] + 60,
        r,
    )
    s = parsers.parse_stressng_yaml(y.read_text())["cpu"]
    r.metric("stressng_cpu_bogo_ops_s", s["bogo_ops_s_real"], "bogo-ops/s", headline=True)


# --- memory ----------------------------------------------------------------


@bench(
    "mem-bw",
    "Memory bandwidth: sysbench memory read/write, 256 MiB blocks, 1 and 4 threads",
    50,
    ["sysbench"],
    params={"seconds": 10, "threads": [1, 4], "block": "256M"},
)
def b_mem_bw(ctx: Ctx, r: Result, p: dict[str, Any]):
    for th in p["threads"]:
        for op in ("read", "write"):
            out = ctx.run(
                f"sysbench-{op}-t{th}",
                [
                    "sysbench", "memory", f"--memory-block-size={p['block']}", "--memory-total-size=100T",
                    f"--memory-oper={op}", f"--threads={th}", f"--time={p['seconds']}", "run",
                ],
                p["seconds"] + 60,
                r,
            )
            m = parsers.parse_sysbench_memory(out)
            r.metric(f"{op}_t{th}_mib_s", m["mib_s"], "MiB/s", headline=(th == max(p["threads"])))


@bench(
    "mem-latency",
    "tinymembench: single-thread bandwidth variants and random-access latency (~3-5 min)",
    300,
    ["tinymembench"],
)
def b_mem_latency(ctx: Ctx, r: Result, p: dict[str, Any]):
    out = ctx.run("tinymembench", ["tinymembench"], 1200, r)
    t = parsers.parse_tinymembench(out)
    for k, v in t["bandwidth_mb_s"].items():
        r.metric(f"bw.{k}", v, "MB/s", headline=k in ("standard_memcpy", "standard_memset"))
    for mode, sizes in t["latency_ns"].items():
        for size, v in sizes.items():
            if size >= 1 << 20:
                big = size == max(sizes)
                r.metric(f"lat.{mode}.{size >> 20}MiB.single_ns", v["single"], "ns", "lower", headline=big and mode == "nohuge")
                r.metric(f"lat.{mode}.{size >> 20}MiB.dual_ns", v["dual"], "ns", "lower")


# --- storage -------------------------------------------------------------

FIO_JOBS = """[global]
filename={file}
size={size}
direct=1
ioengine=libaio
runtime={runtime}
time_based=1
stonewall

[seqwrite]
rw=write
bs=1M
iodepth=16
end_fsync=1

[seqread]
rw=read
bs=1M
iodepth=16

[randread-qd32]
rw=randread
bs=4k
iodepth=32

[randwrite-qd32]
rw=randwrite
bs=4k
iodepth=32

[randread-qd1]
rw=randread
bs=4k
iodepth=1
"""


@bench(
    "nvme",
    "NVMe via fio on a scratch file (not the raw device): seq 1M qd16, rand 4k qd32/qd1",
    70,
    ["fio"],
    params={"size": "1G", "runtime": 10},
    root=True,
)
def b_nvme(ctx: Ctx, r: Result, p: dict[str, Any]):
    scratch = Path(ctx.opts.get("scratch_dir") or ctx.run_dir.parent)
    need = _size_bytes(p["size"]) * 2
    free = shutil.disk_usage(scratch).free
    if free < need:
        raise Skip(f"{scratch}: {free >> 20} MiB free, need {need >> 20}")
    src = subprocess.run(["findmnt", "-no", "SOURCE,FSTYPE", "-T", str(scratch)], capture_output=True, text=True).stdout.strip()
    r.conditions["scratch"] = {"dir": str(scratch), "mount": src}
    f = scratch / "a16-bench-fio.tmp"
    job = ctx.run_dir / "raw" / "fio.job"
    job.write_text(FIO_JOBS.format(file=f, size=p["size"], runtime=p["runtime"]))
    try:
        out = ctx.run("fio", ["fio", "--output-format=json", str(job)], 5 * (p["runtime"] + 60), r)
    finally:
        f.unlink(missing_ok=True)
    jobs = parsers.parse_fio(json.loads(out[out.index("{"):]))
    pick = {
        "seqread": [("read_mb_s", "MB/s", "higher", True)],
        "seqwrite": [("write_mb_s", "MB/s", "higher", True)],
        "randread-qd32": [("read_iops", "IOPS", "higher", True)],
        "randwrite-qd32": [("write_iops", "IOPS", "higher", True)],
        "randread-qd1": [("read_lat_us", "us", "lower", True), ("read_p99_us", "us", "lower", False), ("read_iops", "IOPS", "higher", False)],
    }
    for jn, keys in pick.items():
        res = jobs.get(jn, {})
        if res.get("error"):
            raise RuntimeError(f"fio job {jn} error {res['error']}")
        for k, unit, better, head in keys:
            r.metric(f"{jn}.{k}", res.get(k), unit, better, headline=head)


def _size_bytes(s: str) -> int:
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    s = s.strip().upper()
    return int(float(s[:-1]) * mult[s[-1]]) if s[-1] in mult else int(s)


# --- network -------------------------------------------------------------


@bench(
    "net-iperf3",
    "TCP throughput to an iperf3 server (default lofty305.lan), both directions",
    30,
    ["iperf3"],
    params={"seconds": 10, "port": 5201},
)
def b_iperf3(ctx: Ctx, r: Result, p: dict[str, Any]):
    host = ctx.opts.get("iperf_server") or "lofty305.lan"
    try:
        addr = socket.getaddrinfo(host, p["port"], type=socket.SOCK_STREAM)[0][4][0]
        socket.create_connection((addr, p["port"]), timeout=3).close()
    except OSError as e:
        raise Skip(f"iperf3 server {host}:{p['port']} not reachable ({e})")
    route = subprocess.run(["ip", "route", "get", addr], capture_output=True, text=True).stdout.split()
    r.conditions["server"] = f"{host} ({addr})"
    if "dev" in route:
        r.conditions["interface"] = route[route.index("dev") + 1]
    for name, extra in (("tx", []), ("rx", ["-R"])):
        out = ctx.run(f"iperf3-{name}", ["iperf3", "-c", addr, "-p", str(p["port"]), "-t", str(p["seconds"]), "-J", *extra], p["seconds"] + 60, r)
        m = parsers.parse_iperf3(json.loads(out[out.index("{"):]))
        r.metric(f"{name}_mbit_s", m["mbit_s"], "Mbit/s", headline=True)
        if "retransmits" in m and name == "tx":
            r.metric("tx_retransmits", m["retransmits"], "", "lower")
        time.sleep(1)  # the server takes one client at a time


# --- GPU -----------------------------------------------------------------


def _render_node(ctx: Ctx) -> str:
    node = ctx.probe.render_node()
    if not node:
        raise Skip("no DRM render node")
    return node


def _mark_metrics(r: Result, res: dict[str, Any], prefix: str = ""):
    if parsers.is_software_renderer(res["info"]):
        raise RuntimeError(f"software renderer: {res['info']}")
    r.conditions["renderer"] = res["info"]
    if res["score"] is not None:
        r.metric(f"{prefix}score", res["score"], "score", headline=True)
    for s in res["scenes"]:
        r.metric(f"{prefix}fps.{parsers.scene_key(s)}", s["fps"], "FPS")
    if res["failed"]:
        r.conditions["failed_scenes"] = res["failed"]


@bench(
    "gpu-glmark2",
    "glmark2 (GLES2, GBM on the Adreno render node; no compositor or session needed), default scene set",
    420,
    ["glmark2-es2-gbm"],
    params={"size": "1920x1080"},
)
def b_glmark2(ctx: Ctx, r: Result, p: dict[str, Any]):
    node = _render_node(ctx)
    r.conditions["render_node"] = node
    out = ctx.run("glmark2", ["glmark2-es2-gbm", "--winsys-options", f"drm-device={node}", "-s", p["size"]], 1800, r)
    _mark_metrics(r, parsers.parse_mark(out))


@bench(
    "gpu-vkmark",
    "vkmark (Vulkan, headless window system; no compositor or session needed), default scene set",
    180,
    ["vkmark"],
    params={"size": "1920x1080"},
)
def b_vkmark(ctx: Ctx, r: Result, p: dict[str, Any]):
    out = ctx.run("vkmark", ["vkmark", "--winsys", "headless", "-s", p["size"]], 900, r)
    _mark_metrics(r, parsers.parse_mark(out))


@bench(
    "gpu-sustained",
    "Sustained GPU load: glmark2 terrain at panel size in back-to-back 60 s scenes; FPS per minute + thermal/clock trace",
    330,
    ["glmark2-es2-gbm"],
    params={"minutes": 5, "size": "2880x1800", "scene": "terrain"},
)
def b_gpu_sustained(ctx: Ctx, r: Result, p: dict[str, Any]):
    node = _render_node(ctx)
    cmd = ["glmark2-es2-gbm", "--winsys-options", f"drm-device={node}", "-s", p["size"]]
    for _ in range(p["minutes"]):
        cmd += ["-b", f"{p['scene']}:duration=60"]
    out = ctx.run("glmark2-sustained", cmd, p["minutes"] * 60 + 300, r)
    res = parsers.parse_mark(out)
    if parsers.is_software_renderer(res["info"]):
        raise RuntimeError(f"software renderer: {res['info']}")
    r.conditions["renderer"] = res["info"]
    fps = [s["fps"] for s in res["scenes"]]
    if not fps:
        raise RuntimeError("no scene results")
    for i, v in enumerate(fps, 1):
        r.metric(f"fps.min{i}", v, "FPS")
    r.metric("fps_first", fps[0], "FPS", headline=True)
    r.metric("fps_last", fps[-1], "FPS", headline=True)
    r.metric("fps_drop_pct", 100 * (fps[0] - fps[-1]) / fps[0] if fps[0] else 0, "%", "lower", headline=True)


# --- s2idle ----------------------------------------------------------------

TICKS_HZ = 19_200_000  # qcom_stats accumulated durations are in 19.2 MHz ticks


@bench(
    "s2idle",
    "s2idle residency over N short suspend cycles (qcom_stats deltas), C1/dwc3 USB wake disabled for the run",
    300,
    ["systemctl"],
    params={"cycles": 3, "sleep_s": 60},
    root=True,
    cooldown=False,
    sample=False,
)
def b_s2idle(ctx: Ctx, r: Result, p: dict[str, Any]):
    manual = bool(ctx.opts.get("s2idle_manual"))
    rtc = ctx.probe.wakealarm_rtc()
    if not rtc and not manual:
        raise Skip("no RTC wake alarm (rtc-qcom-glink has no alarm ops); needs a human to wake it: --s2idle-manual")
    if rtc and not shutil.which("rtcwake"):
        raise Skip("missing tools: rtcwake")
    if not ctx.probe.qcom_stats():
        raise Skip("no /sys/kernel/debug/qcom_stats (debugfs not mounted?)")
    mem_sleep = (ctx.probe.p("sys/power/mem_sleep").read_text().strip() if ctx.probe.p("sys/power/mem_sleep").exists() else "")
    r.conditions["mem_sleep"] = mem_sleep
    r.conditions["wake"] = f"rtc {rtc.name}" if rtc else "manual"
    cycles = 1 if manual and not rtc else p["cycles"]

    saved = ctx.probe.usb_wakeup()
    r.conditions["usb_wakeup_before"] = saved
    if ctx.inhibitor:
        ctx.inhibitor.pause()
    try:
        for dev, state in saved.items():
            if state == "enabled":
                Path(dev, "power", "wakeup").write_text("disabled")
        per_cycle = []
        for i in range(1, cycles + 1):
            per_cycle.append(_s2idle_cycle(ctx, r, i, rtc, p["sleep_s"], manual))
            time.sleep(10)
    finally:
        for dev, state in saved.items():
            try:
                Path(dev, "power", "wakeup").write_text(state)
            except OSError as e:
                ctx.log(f"  WARNING: could not restore {dev} wakeup={state}: {e}")
        if ctx.inhibitor:
            ctx.inhibitor.resume()
    r.conditions["usb_wakeup_after"] = ctx.probe.usb_wakeup()
    r.conditions["cycles"] = per_cycle
    ok = [c for c in per_cycle if c.get("suspended_s")]
    r.metric("cycles_ok", len(ok), "", "higher")
    if not ok:
        raise RuntimeError("no cycle reached suspend")
    r.metric("suspended_s_mean", sum(c["suspended_s"] for c in ok) / len(ok), "s", None)
    for stat in sorted({k for c in ok for k in c.get("residency_pct", {})}):
        vals = [c["residency_pct"][stat] for c in ok if stat in c.get("residency_pct", {})]
        r.metric(f"{stat}.residency_pct", sum(vals) / len(vals), "%", "higher", headline=stat in ("cxsd", "ddr", "aosd"))


def _qstats(ctx: Ctx) -> dict[str, dict[str, int]]:
    return {k: parsers.parse_qcom_stat(v) for k, v in ctx.probe.qcom_stats().items()}


def _s2idle_cycle(ctx: Ctx, r: Result, i: int, rtc: Path | None, sleep_s: int, manual: bool) -> dict[str, Any]:
    q0 = _qstats(ctx)
    s0 = parsers.parse_suspend_stats(ctx.probe.suspend_stats())
    if rtc:
        ctx.run(f"s2idle-{i}-rtcwake", ["rtcwake", "-d", rtc.name, "-m", "no", "-s", str(sleep_s)], 30, r)
    else:
        ctx.log("  MANUAL: the A16 suspends now; wake it with a key press or the lid")
    b0, m0 = time.clock_gettime(time.CLOCK_BOOTTIME), time.clock_gettime(time.CLOCK_MONOTONIC)
    subprocess.run(["systemctl", "suspend"], check=False)
    limit = (3600 * 4 if manual else sleep_s + 180)
    deadline = time.monotonic() + limit
    s1 = s0
    while time.monotonic() < deadline:
        time.sleep(2)
        s1 = parsers.parse_suspend_stats(ctx.probe.suspend_stats())
        if s1.get("success", 0) > s0.get("success", 0) or s1.get("fail", 0) > s0.get("fail", 0):
            break
    b1, m1 = time.clock_gettime(time.CLOCK_BOOTTIME), time.clock_gettime(time.CLOCK_MONOTONIC)
    suspended = (b1 - b0) - (m1 - m0)  # CLOCK_MONOTONIC stops in suspend, BOOTTIME doesn't
    q1 = _qstats(ctx)
    wake_irq = ctx.probe.p("sys/power/pm_wakeup_irq")
    c: dict[str, Any] = {
        "cycle": i,
        "success_delta": s1.get("success", 0) - s0.get("success", 0),
        "fail_delta": s1.get("fail", 0) - s0.get("fail", 0),
        "wakeup_irq": wake_irq.read_text().strip() if wake_irq.exists() else None,
    }
    if c["success_delta"] > 0 and suspended > 1:
        c["suspended_s"] = round(suspended, 2)
        c["residency_pct"] = {}
        c["count_delta"] = {}
        for k, a in q1.items():
            b = q0.get(k, {})
            if "accumulated_ticks" in a and "accumulated_ticks" in b:
                d = (a["accumulated_ticks"] - b["accumulated_ticks"]) / TICKS_HZ
                c["residency_pct"][k] = round(100 * d / suspended, 2)
                c["count_delta"][k] = a.get("count", 0) - b.get("count", 0)
    ctx.log(f"  cycle {i}: {c}")
    return c
