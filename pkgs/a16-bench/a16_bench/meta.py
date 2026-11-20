"""Run metadata: what system, kernel, firmware and conditions a run had."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import __version__
from .sysfs import Probe, read


def sh(cmd: list[str], timeout: float = 15) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def tool_versions() -> dict[str, str]:
    """Versions of the bundled tools, baked in by the Nix package."""
    f = os.environ.get("A16_BENCH_TOOL_VERSIONS")
    if f and Path(f).is_file():
        try:
            return json.loads(Path(f).read_text())
        except ValueError:
            pass
    return {}


def generation(probe: Probe) -> dict[str, Any]:
    """Current and booted system store paths and their profile generation numbers."""
    out: dict[str, Any] = {}
    cur = probe.p("run/current-system")
    booted = probe.p("run/booted-system")
    cur_t = os.path.realpath(cur) if cur.exists() else None
    booted_t = os.path.realpath(booted) if booted.exists() else None
    out["current_system"] = cur_t
    out["booted_system"] = booted_t
    out["switched_since_boot"] = bool(cur_t and booted_t and cur_t != booted_t)
    gens: dict[str, int] = {}
    for link in probe.p("nix/var/nix/profiles").glob("system-*-link"):
        m = re.match(r"system-(\d+)-link", link.name)
        if m:
            try:
                gens[os.path.realpath(link)] = int(m[1])
            except OSError:
                pass
    out["current_generation"] = gens.get(cur_t) if cur_t else None
    out["booted_generation"] = gens.get(booted_t) if booted_t else None
    default = probe.p("nix/var/nix/profiles/system")
    if default.exists():
        m = re.match(r"system-(\d+)-link", os.readlink(default)) if default.is_symlink() else None
        out["default_generation"] = int(m[1]) if m else None
    return out


def sessions() -> list[dict[str, str]]:
    """logind sessions (who is logged in where); used for the desktop-in-use guard."""
    out = sh(["loginctl", "list-sessions", "--no-legend"])
    if not out:
        return []
    res = []
    for line in out.splitlines():
        sid = line.split()[0] if line.split() else None
        if not sid:
            continue
        props = sh(["loginctl", "show-session", sid, "-p", "Name", "-p", "Type", "-p", "Class", "-p", "Active", "-p", "IdleHint", "-p", "Remote"])
        d = {"id": sid}
        for p in (props or "").splitlines():
            k, _, v = p.partition("=")
            d[k] = v
        res.append(d)
    return res


def desktop_in_use(sess: list[dict[str, str]]) -> list[dict[str, str]]:
    """Graphical sessions of real users (not the SDDM greeter)."""
    return [
        s
        for s in sess
        if s.get("Type") in ("wayland", "x11") and s.get("Class", "user") == "user" and s.get("Name") not in ("sddm", "gdm", "lightdm")
    ]


def collect(probe: Probe | None = None) -> dict[str, Any]:
    probe = probe or Probe()
    m: dict[str, Any] = {"bench_version": __version__, "tools": tool_versions()}
    m["hostname"] = platform.node()
    u = platform.uname()
    m["kernel"] = {"release": u.release, "version": u.version, "machine": u.machine}
    m["cmdline"] = read(probe.p("proc/cmdline"))
    nv = sh(["nixos-version", "--json"])
    if nv:
        try:
            m["nixos"] = json.loads(nv)
        except ValueError:
            m["nixos"] = {"raw": nv.strip()}
    m["system"] = generation(probe)
    dmi = probe.p("sys/class/dmi/id")
    m["dmi"] = {
        k: read(dmi / k)
        for k in ("sys_vendor", "product_name", "product_version", "board_name", "bios_vendor", "bios_version", "bios_date", "bios_release", "ec_firmware_release")
        if read(dmi / k) is not None
    }
    if not m["dmi"]:
        model = read(probe.p("proc/device-tree/model"))
        if model:
            m["dmi"]["dt_model"] = model.rstrip("\x00")
    m["power"] = probe.power_source()
    m["temps_c"] = probe.temps()
    m["cpu"] = {
        "nproc": os.cpu_count(),
        "policies": {
            p.name: {
                "cpus": p.cpus,
                "max_mhz": (p.max_khz or 0) / 1000,
                "governor": read(p.path / "scaling_governor"),
                "driver": read(p.path / "scaling_driver"),
                "scaling_max_mhz": (int(read(p.path / "scaling_max_freq", "0") or 0)) / 1000,
            }
            for p in probe.cpu_policies()
        },
        "boost": read(probe.p("sys/devices/system/cpu/cpufreq/boost")),
    }
    m["devfreq"] = {
        d.name: {
            "governor": read(d.path / "governor"),
            "min_mhz": int(read(d.path / "min_freq", "0") or 0) / 1e6,
            "max_mhz": int(read(d.path / "max_freq", "0") or 0) / 1e6,
            "available": read(d.path / "available_frequencies"),
        }
        for d in probe.devfreqs()
    }
    m["cooling"] = {
        c.name: {"type": read(c / "type"), "cur_state": read(c / "cur_state"), "max_state": read(c / "max_state")}
        for c in sorted(probe.p("sys/class/thermal").glob("cooling_device*"))
    }
    mem = read(probe.p("proc/meminfo"), "") or ""
    mt = re.search(r"MemTotal:\s+(\d+) kB", mem)
    m["mem_total_mib"] = int(mt[1]) // 1024 if mt else None
    m["display"] = probe.display()
    m["usb_wakeup"] = probe.usb_wakeup()
    m["render_node"] = probe.render_node()
    m["uptime_s"] = float((read(probe.p("proc/uptime"), "0") or "0").split()[0])
    m["loadavg"] = read(probe.p("proc/loadavg"))
    m["sessions"] = sessions()
    rf = sh(["rfkill", "-J"])
    if rf:
        try:
            m["rfkill"] = json.loads(rf)
        except ValueError:
            pass
    pstore = probe.p("var/lib/systemd/pstore")
    m["pstore_entries"] = len(list(pstore.iterdir())) if pstore.is_dir() else None
    return m


def snapshot(probe: Probe | None = None) -> dict[str, Any]:
    """The small before/after state recorded around each test."""
    probe = probe or Probe()
    return {"power": probe.power_source(), "temps_c": probe.temps(), "die_max_c": probe.die_temp_max()}
