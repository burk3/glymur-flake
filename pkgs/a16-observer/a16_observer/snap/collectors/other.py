"""display, suspend, audio, net and power topics. Same rules as usb: sysfs and
software-state debugfs only, commands with a timeout, nothing written."""

from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import re
import struct

from .. import parse
from ..core import Ctx, collector
from ..host import read_kmsg
from .usb import psy_props

# --- display ---------------------------------------------------------------------


@collector("display")
def connectors(ctx: Ctx) -> dict:
    """DRM connectors: status, enabled, dpms, modes, EDID (hash + edid-decode)."""
    out = {}
    for c in ctx.glob("/sys/class/drm/card*-*"):
        name = os.path.basename(c)
        d = ctx.attrs(c, ("status", "enabled", "dpms", "connector_id"))
        modes = (ctx.read(f"{c}/modes") or "").split()
        d["modes"] = modes[:8]
        d["n_modes"] = len(modes)
        edid = ctx.read_bytes(f"{c}/edid") or b""
        d["edid_bytes"] = len(edid)
        if edid:
            d["edid_sha"] = hashlib.sha256(edid).hexdigest()[:16]
            ctx.raw(f"edid-hex_{name}", edid.hex())
            r = ctx.run(["edid-decode"], input=edid, raw=f"edid-decode_{name}")
            if r.out:
                m = re.search(r"Display Product Name: '([^']*)'", r.out)
                d["edid_product"] = m.group(1) if m else None
                m = re.search(r"Manufacturer: (\S+)", r.out)
                d["edid_manufacturer"] = m.group(1) if m else None
        out[name] = d
    return out


@collector("display")
def drm_debug(ctx: Ctx) -> dict:
    """msm DP/eDP link (dp_debug: lanes, rate, bw_code, levels), atomic state
    (active CRTC modes), from software-state debugfs only."""
    out: dict = {}
    for card in ctx.glob("/sys/kernel/debug/dri/*"):
        cn = os.path.basename(card)
        if not cn.isdigit():
            continue
        name = ctx.read(f"{card}/name")
        entry: dict = {"name": name}
        for conn in ctx.listdir(card):
            if ctx.exists(f"{card}/{conn}/dp_debug"):
                t = ctx.read(f"{card}/{conn}/dp_debug")
                if t is not None:
                    ctx.raw(f"dp_debug_{cn}_{conn}", t)
                    entry[conn] = parse.dp_debug(t)
        st = ctx.read(f"{card}/state")
        if st:
            ctx.raw(f"drm-state_{cn}", st)
            crtcs = {}
            for m in re.finditer(r"crtc\[(\d+)\]: (\S+)\n\s+enable=(\d)\n\s+active=(\d)(?:.*?\n\s+mode: \"([^\"]*)\": (\d+) (\d+))?", st, re.S):
                crtcs[m.group(2)] = {"enable": int(m.group(3)), "active": int(m.group(4)), "mode": m.group(5), "vrefresh": m.group(6)}
            entry["crtcs"] = crtcs
            conns = {}
            for m in re.finditer(r"connector\[(\d+)\]: (\S+)\n\s+crtc=(\S+)", st):
                conns[m.group(2)] = m.group(3)
            entry["connector_crtc"] = conns
        out[cn] = entry
    return out


@collector("display")
def backlight(ctx: Ctx) -> dict:
    """Panel backlight brightness, actual, max, bl_power."""
    return {os.path.basename(b): ctx.attrs(b, ("brightness", "actual_brightness", "max_brightness", "bl_power", "type")) for b in ctx.glob("/sys/class/backlight/*")}


@collector("display")
def display_klog(ctx: Ctx) -> dict:
    """Kernel log lines for msm/dp/dpu/drm/typec-altmode in this boot (raw)."""
    rx = re.compile(r"\bmsm|\bdp\b|dpu|drm|edp|altmode|hpd|link train", re.I)
    lines = [parse.fmt_kmsg(r) for r in read_kmsg(ctx) if rx.search(r["msg"])]
    ctx.raw("klog-display", "\n".join(lines))
    err_rx = re.compile(r"\berror\b|\bfailed\b(?! attempts)|timed? ?out|: -\d+\s*$", re.I)
    errs = [l for l in lines if err_rx.search(l) and "Kernel command line" not in l]
    return {"lines": len(lines), "error_lines": len(errs), "last_errors": errs[-10:]}


# --- suspend ----------------------------------------------------------------------
# NOT /sys/power/wakeup_count: reading it blocks until no wakeup event is in
# flight.
SYS_POWER = ("state", "mem_sleep", "disk", "pm_async", "pm_wakeup_irq", "pm_test", "pm_print_times", "pm_debug_messages", "sync_on_suspend", "wake_lock", "pm_freeze_timeout")


@collector("suspend")
def sys_power(ctx: Ctx) -> dict:
    """/sys/power settings, mem_sleep, pm_wakeup_irq, suspend_stats."""
    d = ctx.attrs("/sys/power", SYS_POWER)
    d["mem_sleep_selected"] = parse.bracketed(d.get("mem_sleep"))
    d["suspend_stats"] = {os.path.basename(f): ctx.read(f) for f in ctx.glob("/sys/power/suspend_stats/*")}
    t = ctx.read("/sys/kernel/debug/suspend_stats")
    if t:
        ctx.raw("suspend_stats", t)
    return d


@collector("suspend")
def qcom_stats(ctx: Ctx) -> dict:
    """qcom_stats sleep residency and counts (CXSD, AOSD, DDR, subsystems), DDR stats."""
    out = {}
    for f in ctx.glob("/sys/kernel/debug/qcom_stats/*"):
        n = os.path.basename(f)
        t = ctx.read(f)
        if t is None:
            continue
        out[n] = parse.ddr_stats(t) if n == "ddr_stats" else parse.qcom_stats(t)
    return out


@collector("suspend")
def wakeup(ctx: Ctx) -> dict:
    """Wakeup sources with any activity (debugfs wakeup_sources)."""
    t = ctx.read("/sys/kernel/debug/wakeup_sources")
    if t:
        ctx.raw("wakeup_sources", t)
    return parse.wakeup_sources(t or "")


EVIOCGSW = lambda n: (2 << 30) | (n << 16) | (ord("E") << 8) | 0x1B  # noqa: E731
EVIOCGBIT_SW = lambda n: (2 << 30) | (n << 16) | (ord("E") << 8) | (0x20 + 0x05)  # noqa: E731
SW_LID = 0


@collector("suspend")
def lid(ctx: Ctx) -> dict:
    """Lid switch state (EVIOCGSW on the gpio-keys input device, read-only open)."""
    out = {}
    for ev in ctx.glob("/sys/class/input/event*"):
        name = ctx.read(f"{ev}/device/name") or ""
        sw = ctx.read(f"{ev}/device/capabilities/sw") or "0"
        try:
            has_lid = int(sw.split()[-1], 16) & (1 << SW_LID)
        except ValueError:
            has_lid = 0
        if not has_lid:
            continue
        dev = f"/dev/input/{os.path.basename(ev)}"
        state = None
        if not ctx.root:
            try:
                fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                try:
                    buf = fcntl.ioctl(fd, EVIOCGSW(8), b"\0" * 8)
                    state = "closed" if struct.unpack("Q", buf)[0] & (1 << SW_LID) else "open"
                finally:
                    os.close(fd)
            except OSError as e:
                state = f"error: {e}"
        out[name or dev] = {"device": dev, "lid": state}
    return out


@collector("suspend")
def sleep_journal(ctx: Ctx) -> dict:
    """systemd-sleep / suspend journal of this boot, and the kernel's PM lines."""
    r = ctx.run(["journalctl", "-b", "--no-pager", "-o", "short-iso", "-n", "60", "-t", "systemd-sleep", "-t", "systemd-logind", "-u", "systemd-suspend.service"], raw="journal-sleep")
    rx = re.compile(r"^PM: |suspend|resume|Freezing|Restarting tasks|wakeup|s2idle", re.I)
    lines = [parse.fmt_kmsg(x) for x in read_kmsg(ctx) if rx.search(x["msg"])]
    ctx.raw("klog-pm", "\n".join(lines))
    entries = [l for l in lines if "suspend entry" in l]
    exits = [l for l in lines if "suspend exit" in l]
    return {"journal_rc": r.rc, "suspend_entries": len(entries), "suspend_exits": len(exits), "last_entry": entries[-1] if entries else None, "last_exit": exits[-1] if exits else None}


# --- audio ------------------------------------------------------------------------
@collector("audio")
def alsa(ctx: Ctx) -> dict:
    """ALSA cards, PCMs and every card's mixer contents (amixer, read)."""
    cards = ctx.read("/proc/asound/cards") or ""
    ctx.raw("asound-cards", cards)
    pcm = ctx.read("/proc/asound/pcm") or ""
    ctx.raw("asound-pcm", pcm)
    out: dict = {"cards": {}, "pcm": [l.split(":")[0].strip() + " " + l.split(":")[1].strip() for l in pcm.splitlines() if ":" in l]}
    for m in re.finditer(r"^\s*(\d+) \[(\S+)\s*\]: (.*)$", cards, re.M):
        idx = m.group(1)
        r = ctx.run(["amixer", "-c", idx, "contents"], raw=f"amixer_{idx}")
        mixer = parse.amixer_contents(r.out) if r.ok else r.summary()
        streams = {}
        for s in ctx.glob(f"/proc/asound/card{idx}/pcm*/sub*/status"):
            streams[s.split(f"card{idx}/")[1]] = (ctx.read(s) or "").splitlines()[0] if ctx.read(s) else None
        out["cards"][m.group(2)] = {"index": int(idx), "desc": m.group(3).strip(), "controls": mixer, "substreams": streams}
    return out


@collector("audio")
def soundwire(ctx: Ctx) -> dict:
    """SoundWire peripherals: status, device number, driver, runtime PM (sysfs only)."""
    out = {}
    for d in ctx.glob("/sys/bus/soundwire/devices/*"):
        n = os.path.basename(d)
        x = ctx.attrs(d, ("status", "device_number", "modalias"))
        x["driver"] = ctx.link_name(f"{d}/driver")
        x["runtime_status"] = ctx.runtime_status(d)
        out[n] = x
    return out


@collector("audio")
def topology(ctx: Ctx) -> dict:
    """Which topology/firmware files the audio stack loaded (kernel log) and their hashes."""
    rx = re.compile(r"tplg|topology|audioreach|\.bin\b.*(?:adsp|audio)|q6apm|q6prm|snd-soc", re.I)
    lines = [parse.fmt_kmsg(r) for r in read_kmsg(ctx) if rx.search(r["msg"])]
    ctx.raw("klog-audio", "\n".join(lines))
    files = {}
    for pat in ("/run/current-system/firmware/qcom/**/*tplg*", "/run/current-system/firmware/qcom/**/*topology*"):
        for f in glob.glob(ctx.p(pat), recursive=True):
            f = ctx.unp(f)
            b = ctx.read_bytes(f, limit=16 << 20)
            if b is not None:
                files[f.removeprefix("/run/current-system/firmware/")] = hashlib.sha256(b).hexdigest()[:16]
    return {"files": files, "klog_lines": len(lines), "klog_tail": lines[-8:]}


@collector("audio")
def pipewire(ctx: Ctx) -> dict:
    """PipeWire nodes (pw-dump as the session user) when a user session runs; else skipped."""
    out = {}
    for rd in ctx.glob("/run/user/*"):
        uid = os.path.basename(rd)
        if not uid.isdigit() or uid == "0" or not ctx.exists(f"{rd}/pipewire-0"):
            continue
        r = ctx.run(["setpriv", f"--reuid={uid}", f"--regid={uid}", "--init-groups", "env", f"XDG_RUNTIME_DIR={rd}", "pw-dump", "--no-colors"], raw=f"pw-dump_{uid}")
        if not r.ok:
            out[uid] = r.summary()
            continue
        try:
            objs = json.loads(r.out)
        except ValueError as e:
            out[uid] = {"error": f"pw-dump json: {e}"}
            continue
        nodes = {}
        for o in objs:
            if o.get("type") != "PipeWire:Interface:Node":
                continue
            info = o.get("info") or {}
            props = info.get("props") or {}
            nodes[props.get("node.name", str(o.get("id")))] = {"id": o.get("id"), "media_class": props.get("media.class"), "state": info.get("state"), "description": props.get("node.description")}
        out[uid] = {"nodes": nodes}
    return out or {"skipped": "no user session with a PipeWire socket"}


# --- net --------------------------------------------------------------------------
@collector("net")
def links(ctx: Ctx) -> dict:
    """All links: carrier, operstate, speed/duplex, driver, bus, addresses; routes."""
    out = {}
    for n in ctx.glob("/sys/class/net/*"):
        ifn = os.path.basename(n)
        d = ctx.attrs(n, ("carrier", "operstate", "speed", "duplex", "mtu", "address", "carrier_changes"))
        d["driver"] = ctx.link_name(f"{n}/device/driver")
        dev = ctx.realpath(f"{n}/device") if ctx.exists(f"{n}/device") else None
        d["bus_path"] = dev
        out[ifn] = d
    r = ctx.run(["ip", "-j", "addr"], raw="ip-addr")
    try:
        for o in json.loads(r.out) if r.ok else []:
            if o.get("ifname") in out:
                out[o["ifname"]]["addr"] = [f"{a.get('local')}/{a.get('prefixlen')}" for a in o.get("addr_info", [])]
    except ValueError:
        pass
    routes = ctx.run(["ip", "route"], raw="ip-route")
    ctx.run(["ip", "-6", "route"], raw="ip6-route")
    return {"links": out, "routes4": routes.out.splitlines() if routes.ok else routes.summary()}


@collector("net")
def networkmanager(ctx: Ctx) -> dict:
    """NetworkManager device status (nmcli -t, read)."""
    r = ctx.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], raw="nmcli-device")
    if not r.ok:
        return r.summary()
    out = {}
    for l in r.out.splitlines():
        f = l.split(":")
        if len(f) >= 4:
            out[f[0]] = {"type": f[1], "state": f[2], "connection": ":".join(f[3:])}
    return out


@collector("net")
def wifi(ctx: Ctx) -> dict:
    """Wi-Fi link (iw dev link) and station dump."""
    out = {}
    for n in ctx.glob("/sys/class/net/*"):
        if not ctx.exists(f"{n}/wireless") and not ctx.exists(f"{n}/phy80211"):
            continue
        ifn = os.path.basename(n)
        r = ctx.run(["iw", "dev", ifn, "link"], raw=f"iw-link_{ifn}")
        d: dict = {"link": parse.iw_link(r.out) if r.ok else r.summary()}
        ctx.run(["iw", "dev", ifn, "station", "dump"], raw=f"iw-station_{ifn}")
        d["phy"] = ctx.link_name(f"{n}/phy80211")
        out[ifn] = d
    return out


# --- power ------------------------------------------------------------------------
NAMED_CLOCKS = re.compile(r"^gcc_(usb|cfg_noc_usb|aggre_usb|usb3|usb4|usb30|usb20)")


@collector("power")
def supplies(ctx: Ctx) -> dict:
    """Every power_supply (uevent: voltage, current, online, type, status...)."""
    out = {}
    for d in ctx.glob("/sys/class/power_supply/*"):
        out[os.path.basename(d)] = psy_props(parse.uevent(ctx.read(f"{d}/uevent") or ""))
    return out


@collector("power")
def thermal(ctx: Ctx) -> dict:
    """Thermal zones (type, temp, mode) and cooling devices' current state."""
    zones = {}
    for z in ctx.glob("/sys/class/thermal/thermal_zone*"):
        a = ctx.attrs(z, ("type", "temp", "mode", "policy"))
        zones[a.pop("type", os.path.basename(z))] = a
    cool = {}
    for c in ctx.glob("/sys/class/thermal/cooling_device*"):
        a = ctx.attrs(c, ("type", "cur_state", "max_state"))
        cool[f"{os.path.basename(c)} {a.pop('type', '')}".strip()] = a
    return {"zones": zones, "cooling": cool}


@collector("power")
def cpufreq(ctx: Ctx) -> dict:
    """cpufreq policies: governor, current/min/max (scaling_* only)."""
    out = {}
    for p in ctx.glob("/sys/devices/system/cpu/cpufreq/policy*"):
        out[os.path.basename(p)] = ctx.attrs(p, ("related_cpus", "scaling_governor", "scaling_cur_freq", "scaling_min_freq", "scaling_max_freq", "cpuinfo_max_freq"))
    return out


@collector("power")
def regulators(ctx: Ctx) -> dict:
    """regulator_summary (all regulators: use counts, opmode, voltage, consumers)."""
    t = ctx.read("/sys/kernel/debug/regulator/regulator_summary")
    if t is None:
        return {"present": False}
    ctx.raw("regulator_summary", t)
    return parse.regulator_summary(t)


@collector("power")
def genpd(ctx: Ctx) -> dict:
    """Power domains (pm_genpd_summary): status and attached devices' runtime state."""
    t = ctx.read("/sys/kernel/debug/pm_genpd/pm_genpd_summary")
    if t is None:
        return {"present": False}
    ctx.raw("pm_genpd_summary", t)
    return parse.genpd_summary(t)


@collector("power")
def clocks(ctx: Ctx) -> dict:
    """Named clocks (USB GCC branches): enable/prepare counts and rate. Not
    clk_summary: that runtime-resumes every clock controller to read it."""
    out = {}
    for c in ctx.glob("/sys/kernel/debug/clk/*"):
        n = os.path.basename(c)
        if not NAMED_CLOCKS.match(n):
            continue
        out[n] = {k: ctx.read_int(f"{c}/{k}") for k in ("clk_enable_count", "clk_prepare_count", "clk_rate")}
    return out
