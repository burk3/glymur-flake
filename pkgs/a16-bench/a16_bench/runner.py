"""Profiles, planning, and running a profile into a results directory."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from . import meta as metamod
from .benchmarks import REGISTRY, Aborted, Ctx, Result, Skip
from .sampler import Sampler
from .sysfs import Probe

SCHEMA = 1
DEFAULT_ROOT = Path("/var/lib/a16-bench")

# Profile = ordered (benchmark, param overrides). Order matters: idle power
# first (coolest, least disturbed), s2idle late (it can drop the USB NIC),
# the opt-in all-core load last (if it resets the A16, everything before it
# is already on disk).
PROFILES: dict[str, dict[str, Any]] = {
    "quick": {
        "about": "default milestone set, ~16 min, no human, AC or battery",
        "tests": [
            ("boot", {}),
            ("idle-power", {"seconds": 60}),
            ("cpu-single", {}),
            ("mem-bw", {}),
            ("nvme", {}),
            ("gpu-vkmark", {}),
            ("gpu-glmark2", {}),
        ],
    },
    "full": {
        "about": "quick + tinymembench, iperf3, sustained GPU, s2idle; ~35-45 min",
        "tests": [
            ("boot", {}),
            ("idle-power", {"seconds": 300}),
            ("cpu-single", {}),
            ("mem-bw", {}),
            ("mem-latency", {}),
            ("nvme", {}),
            ("net-iperf3", {}),
            ("gpu-vkmark", {}),
            ("gpu-glmark2", {}),
            ("gpu-sustained", {}),
            ("s2idle", {}),
            ("cpu-multi", {}),
        ],
    },
    "gpu": {
        "about": "GPU/display changes: vkmark, glmark2, 5 min sustained load; ~15 min",
        "tests": [("gpu-vkmark", {}), ("gpu-glmark2", {}), ("gpu-sustained", {})],
    },
    "power": {
        "about": "power/thermal changes: 5 min idle power (battery), s2idle cycles",
        "tests": [("idle-power", {"seconds": 300}), ("s2idle", {})],
    },
    "cpu": {
        "about": "CPU/memory: single-thread per cluster, memory bandwidth/latency, opt-in all-core",
        "tests": [("cpu-single", {}), ("mem-bw", {}), ("mem-latency", {}), ("cpu-multi", {})],
    },
}


def plan(profile: str, only: list[str] | None = None, skip: list[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    if profile not in PROFILES:
        raise SystemExit(f"unknown profile {profile!r}; one of {', '.join(PROFILES)}")
    tests = list(PROFILES[profile]["tests"])
    if only:
        unknown = [t for t in only if t not in REGISTRY]
        if unknown:
            raise SystemExit(f"unknown test(s): {', '.join(unknown)}")
        names = [t for t, _ in tests]
        # --only may name tests outside the profile; they run with defaults.
        tests = [(t, dict(tests[names.index(t)][1]) if t in names else {}) for t in only]
    if skip:
        tests = [(t, p) for t, p in tests if t not in skip]
    return tests


class Inhibitor:
    """Blocks sleep and lid handling for the run (systemd-inhibit), so idle
    timers and the lid can't suspend the A16 mid-benchmark."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def resume(self) -> bool:
        if self.proc:
            return True
        if not shutil.which("systemd-inhibit"):
            return False
        self.proc = subprocess.Popen(
            ["systemd-inhibit", "--what=sleep:handle-lid-switch", "--who=a16-bench", "--why=benchmark run", "--mode=block", "sleep", "infinity"],
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)
        if self.proc.poll() is not None:  # e.g. not root and no polkit rule
            self.proc = None
            return False
        return True

    def pause(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()
            self.proc = None


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def cooldown(probe: Probe, target_c: float, timeout_s: float, log) -> dict[str, Any]:
    t0 = time.monotonic()
    temp = probe.die_temp_max()
    while temp is not None and temp > target_c and time.monotonic() - t0 < timeout_s:
        time.sleep(5)
        temp = probe.die_temp_max()
    waited = round(time.monotonic() - t0, 1)
    res = {"target_c": target_c, "waited_s": waited, "start_die_max_c": temp}
    if temp is not None and temp > target_c:
        res["timed_out"] = True
        log(f"  cooldown: still {temp} C after {waited} s (target {target_c}); starting anyway")
    elif waited > 1:
        log(f"  cooldown: {temp} C after {waited} s")
    return res


def run_profile(args) -> Path:
    probe = Probe()
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    lock = open(root / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another a16-bench run holds {root}/.lock")

    tests = plan(args.profile, args.only, args.skip)
    m = metamod.collect(probe)
    busy = metamod.desktop_in_use(m["sessions"])
    if busy and not args.allow_session:
        raise SystemExit(
            "a desktop session is active (" + ", ".join(f"{s.get('Name')} {s.get('Type')}" for s in busy) + "); "
            "the user may be using the A16. Re-run with --allow-session if that is known to be fine."
        )
    gen = m["system"].get("booted_generation")
    label = args.label or f"{args.profile}" + (f"-gen{gen}" if gen else "")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{label}"
    run_dir = root / run_id
    run_dir.mkdir()
    logf = open(run_dir / "log.txt", "a")

    def log(msg: str):
        line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    opts = {
        "allow_allcore": args.allow_allcore,
        "iperf_server": args.iperf_server,
        "s2idle_manual": args.s2idle_manual,
        "scratch_dir": args.scratch_dir,
    }
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "label": label,
        "profile": args.profile,
        "argv": args.argv,
        "started": now_iso(),
        "finished": None,
        "complete": False,
        "meta": m,
        "tests": {},
    }

    def save():
        tmp = run_dir / "results.json.tmp"
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=False))
        tmp.replace(run_dir / "results.json")

    save()
    log(f"a16-bench {m['bench_version']} run {run_id}: {', '.join(t for t, _ in tests)}")
    log(f"kernel {m['kernel']['release']}, generation {gen}, power {m['power']['source']}")
    ctx = Ctx(run_dir, probe, opts, log)
    inh = Inhibitor()
    ctx.inhibitor = inh
    if not inh.resume():
        log("WARNING: could not take a sleep/lid inhibitor; idle suspend or the lid may interrupt the run")
    completed = False
    try:
        for name, overrides in tests:
            b = REGISTRY[name]
            params = {**b.params, **overrides, **(args.param.get(name, {}) if args.param else {})}
            r = Result()
            started = time.monotonic()
            log(f"== {name}: {b.title}")
            reason = b.precheck(opts, probe)
            if reason:
                r.status, r.reason = "skipped", reason
                log(f"  skipped: {reason}")
            else:
                if b.cooldown and not args.no_cooldown:
                    r.conditions["cooldown"] = cooldown(probe, args.cooldown_c, args.cooldown_timeout, log)
                r.conditions["before"] = metamod.snapshot(probe)
                r.conditions["params"] = params
                doc["tests"][name] = {**r.to_json(), "status": "running", "started": now_iso()}
                save()
                sampler = None
                try:
                    if b.sample:
                        sampler = Sampler(ctx.channels, run_dir / "traces" / f"{name}.csv", b.sample_interval, args.abort_c)
                        ctx.sampler = sampler
                        with sampler:
                            b.fn(ctx, r, params)
                    else:
                        b.fn(ctx, r, params)
                except Skip as e:
                    r.status, r.reason = "skipped", str(e)
                    log(f"  skipped: {e}")
                except Aborted as e:
                    r.status, r.reason = "aborted", str(e)
                    log(f"  ABORTED: {e}")
                except Exception as e:  # noqa: BLE001 - one test's failure must not end the run
                    r.status, r.reason = "failed", f"{type(e).__name__}: {e}"
                    (run_dir / "raw" / f"{name}-traceback.txt").write_text(traceback.format_exc())
                    log(f"  FAILED: {r.reason}")
                finally:
                    ctx.sampler = None
                if sampler is not None:
                    r.trace = {"file": f"traces/{name}.csv", **sampler.summary()}
                    for k in ("temp_die_max_c", "temp_cpu_max_c", "temp_gpu_max_c", "cpu_capped_s", "gpu_capped_s", "gpu_mhz_mean", "fan_rpm_max"):
                        if k in r.trace and r.status == "ok":
                            r.metric(f"trace.{k}", r.trace[k], _unit(k), None)
                r.conditions["after"] = metamod.snapshot(probe)
            doc["tests"][name] = {**r.to_json(), "duration_s": round(time.monotonic() - started, 1)}
            if r.status == "ok":
                heads = {k: v["value"] for k, v in r.metrics.items() if v.get("headline")}
                log(f"  ok: {heads}")
            save()
        completed = True
    finally:
        inh.pause()
        doc["finished"] = now_iso()
        # False here (or no "finished" at all after a reset) marks a partial run.
        doc["complete"] = completed
        doc["meta_after"] = metamod.snapshot(probe)
        save()
        with open(root / "index.jsonl", "a") as idx:
            idx.write(json.dumps(summary_line(doc)) + "\n")
        log(f"results: {run_dir}/results.json")
    return run_dir


def _unit(k: str) -> str:
    return "C" if k.endswith("_c") else "s" if k.endswith("_s") else "MHz" if "mhz" in k else "rpm" if "rpm" in k else ""


def summary_line(doc: dict[str, Any]) -> dict[str, Any]:
    m = doc.get("meta", {})
    tests = doc.get("tests", {})
    return {
        "run_id": doc.get("run_id"),
        "label": doc.get("label"),
        "profile": doc.get("profile"),
        "started": doc.get("started"),
        "finished": doc.get("finished"),
        "kernel": m.get("kernel", {}).get("release"),
        "generation": m.get("system", {}).get("booted_generation"),
        "revision": (m.get("nixos") or {}).get("configurationRevision"),
        "power": m.get("power", {}).get("source"),
        "status": {n: t.get("status") for n, t in tests.items()},
        "headline": {
            f"{n}.{k}": v["value"] for n, t in tests.items() for k, v in (t.get("metrics") or {}).items() if v.get("headline")
        },
    }
