"""Read-only views of the sysfs state the bench samples and records.

Everything goes through Probe(root), so tests can point it at a fake tree.
Nothing here writes to sysfs; the few writes the bench does (USB wakeup for
the s2idle test) live in benchmarks.py next to their restore.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .parsers import parse_uevent

# Thermal zones that are not die temperatures: PMIC temp alarms report stages
# and stick at the last one (the A16's PMH0104 reads 93 at idle; README), the
# battery/charger and radios have their own scales. They are still sampled,
# but they don't count toward the SoC maximum (cooldown, abort, summaries).
_NON_DIE_ZONE = re.compile(r"^(pm.*|smb.*|.*temp-?alarm.*|.*batt.*|.*charger.*|ath\d+.*|.*wifi.*|acpitz|.*skin.*|ec[-_].*|asus.*)$", re.I)


def read(path: Path, default: str | None = None) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return default


def read_int(path: Path) -> int | None:
    v = read(path)
    if v is None:
        return None
    try:
        return int(v.split()[0])
    except (ValueError, IndexError):
        return None


@dataclass
class Zone:
    name: str  # thermal_zoneN
    type: str
    path: Path

    @property
    def die(self) -> bool:
        return not _NON_DIE_ZONE.match(self.type)

    @property
    def kind(self) -> str:
        t = self.type.lower()
        if t.startswith("gpu"):
            return "gpu"
        if t.startswith("cpu"):
            return "cpu"
        return "soc" if self.die else "other"


@dataclass
class CpuPolicy:
    name: str  # policyN
    path: Path
    cpus: list[int]
    max_khz: int | None


@dataclass
class Devfreq:
    name: str
    path: Path


class Probe:
    def __init__(self, root: str | Path = "/"):
        self.root = Path(root)

    def p(self, rel: str) -> Path:
        return self.root / rel.lstrip("/")

    # --- discovery ------------------------------------------------------

    def zones(self) -> list[Zone]:
        out = []
        for d in sorted(self.p("sys/class/thermal").glob("thermal_zone*"), key=_natkey):
            t = read(d / "type")
            if t is not None:
                out.append(Zone(d.name, t, d))
        return out

    def cpu_policies(self) -> list[CpuPolicy]:
        out = []
        for d in sorted(self.p("sys/devices/system/cpu/cpufreq").glob("policy*"), key=_natkey):
            cpus = _cpulist(read(d / "related_cpus", "") or read(d / "affected_cpus", "") or "")
            out.append(CpuPolicy(d.name, d, cpus, read_int(d / "cpuinfo_max_freq")))
        return out

    def devfreqs(self) -> list[Devfreq]:
        return [Devfreq(d.name, d) for d in sorted(self.p("sys/class/devfreq").glob("*"))]

    def gpu_devfreq(self) -> Devfreq | None:
        for d in self.devfreqs():
            if d.name.endswith(".gpu") or "gpu" in d.name:
                return d
        return None

    def fans(self) -> list[tuple[str, Path]]:
        out = []
        for h in sorted(self.p("sys/class/hwmon").glob("hwmon*"), key=_natkey):
            name = read(h / "name", h.name)
            for f in sorted(h.glob("fan*_input")):
                out.append((f"{name}.{f.name.removesuffix('_input')}", f))
        return out

    def power_supplies(self) -> dict[str, dict[str, str]]:
        out = {}
        for d in sorted(self.p("sys/class/power_supply").glob("*")):
            ue = read(d / "uevent")
            if ue is None:
                continue
            info = parse_uevent(ue)
            if "type" not in info:
                info["type"] = read(d / "type", "") or ""
            out[d.name] = info
        return out

    def battery(self) -> tuple[str, Path] | None:
        for d in sorted(self.p("sys/class/power_supply").glob("*")):
            if read(d / "type") == "Battery" and read(d / "scope") != "Device":
                return d.name, d
        return None

    def render_node(self) -> str | None:
        """The GPU's DRM render node (/dev/dri/renderDN). With
        msm.separate_gpu_kms=1 the Adreno (3d00000.gpu) has its own card."""
        nodes = sorted(self.p("sys/class/drm").glob("renderD*"), key=_natkey)
        best = None
        for n in nodes:
            try:
                dev = os.path.realpath(n / "device")
            except OSError:
                dev = ""
            if ".gpu" in dev:
                return f"/dev/dri/{n.name}"
            best = best or f"/dev/dri/{n.name}"
        return best

    # --- state ----------------------------------------------------------

    def power_source(self) -> dict[str, object]:
        """{source: ac|battery|unknown, ac_online, battery: {status, capacity, ...}}."""
        ps = self.power_supplies()
        ac = any(
            i.get("online") == "1" for n, i in ps.items() if i.get("type") in ("Mains", "USB", "USB_PD", "USB_C")
        )
        bat = next((i for i in ps.values() if i.get("type") == "Battery" and i.get("scope") != "Device"), None)
        status = bat.get("status") if bat else None
        if status == "Discharging":
            src = "battery"
        elif ac or status in ("Charging", "Full", "Not charging"):
            src = "ac"
        else:
            src = "unknown"
        out: dict[str, object] = {"source": src, "ac_online": ac}
        if bat:
            out["battery"] = {
                k: bat.get(k)
                for k in ("status", "capacity", "energy_now", "energy_full", "energy_full_design", "power_now", "voltage_now", "cycle_count")
                if k in bat
            }
        return out

    def temps(self) -> dict[str, float]:
        """{zone type (deduplicated): degC} for every readable zone."""
        out: dict[str, float] = {}
        for z in self.zones():
            v = read_int(z.path / "temp")
            if v is None:
                continue
            key = z.type if z.type not in out else f"{z.type}#{z.name}"
            out[key] = v / 1000
        return out

    def die_temp_max(self) -> float | None:
        vals = []
        for z in self.zones():
            if z.die:
                v = read_int(z.path / "temp")
                if v is not None:
                    vals.append(v / 1000)
        return max(vals) if vals else None

    def usb_wakeup(self) -> dict[str, str]:
        """dwc3 controllers' power/wakeup (a600000.usb is C1; README)."""
        out = {}
        for d in sorted(self.p("sys/bus/platform/devices").glob("*.usb")):
            v = read(d / "power" / "wakeup")
            if v is not None:
                out[str(d)] = v
        return out

    def wakealarm_rtc(self) -> Path | None:
        """An RTC that can wake the system (its wakealarm attribute only exists
        when the driver has alarm ops). rtc-qcom-glink has none (README)."""
        for r in sorted(self.p("sys/class/rtc").glob("rtc*"), key=_natkey):
            if (r / "wakealarm").exists():
                return r
        return None

    def qcom_stats(self) -> dict[str, str]:
        d = self.p("sys/kernel/debug/qcom_stats")
        out = {}
        if d.is_dir():
            for f in sorted(d.iterdir()):
                v = read(f)
                if v is not None:
                    out[f.name] = v
        return out

    def suspend_stats(self) -> dict[str, str]:
        d = self.p("sys/power/suspend_stats")
        out = {}
        if d.is_dir():
            for f in sorted(d.iterdir()):
                v = read(f)
                if v is not None:
                    out[f.name] = v
        return out

    def display(self) -> dict[str, object]:
        out: dict[str, object] = {}
        for c in sorted(self.p("sys/class/drm").glob("card*-*")):
            st = read(c / "status")
            if st is None:
                continue
            out[c.name] = {"status": st, "enabled": read(c / "enabled"), "dpms": read(c / "dpms")}
        bl = {}
        for b in sorted(self.p("sys/class/backlight").glob("*")):
            bl[b.name] = {
                "brightness": read_int(b / "brightness"),
                "actual_brightness": read_int(b / "actual_brightness"),
                "max_brightness": read_int(b / "max_brightness"),
                "bl_power": read_int(b / "bl_power"),
            }
        out["backlight"] = bl
        return out


def _natkey(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def _cpulist(s: str) -> list[int]:
    """'0-5' or '0 1 2' or '0-3,8' -> [ints]."""
    out: list[int] = []
    for part in re.split(r"[,\s]+", s.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out
