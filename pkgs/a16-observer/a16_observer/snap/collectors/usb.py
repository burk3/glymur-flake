"""usb: Type-C class, UCSI, power, USB tree, dwc3/xHCI, PHYs, IRQs, kernel log,
and the NICs and disks hanging off the selected port(s).

Every collector returns {port: data} for the ports in ctx.port ("all": c1, c2,
a1). The port's xHCI device, buses, typec port, PHYs and repeaters come from
sysfs links (`port_info`), not from a hard-coded table."""

from __future__ import annotations

import hashlib
import os
import re

from .. import parse
from ..core import PORTS, Ctx, collector
from ..host import read_kmsg

# Allowlists: only attributes known to be cached software state. Notably NOT
# usbN-portM/disable (its show runs GET_PORT_STATUS and autoresumes the hub).
DEV_ATTRS = (
    "idVendor", "idProduct", "bcdDevice", "manufacturer", "product", "serial",
    "speed", "version", "rx_lanes", "tx_lanes", "bMaxPower", "bNumConfigurations",
    "bConfigurationValue", "configuration", "bDeviceClass", "bDeviceSubClass",
    "bDeviceProtocol", "maxchild", "devnum", "busnum", "devpath", "removable",
    "quirks", "ltm_capable", "authorized", "avoid_reset_quirk", "urbnum",
    "authorized_default", "interface_authorized_default",
)  # fmt: skip
DEV_POWER_ATTRS = (
    "runtime_status", "control", "autosuspend_delay_ms", "persist", "wakeup",
    "usb2_hardware_lpm", "usb2_lpm_l1_timeout", "usb2_lpm_besl",
    "usb3_hardware_lpm_u1", "usb3_hardware_lpm_u2", "runtime_usage",
    "runtime_active_kids", "runtime_enabled", "connected_duration", "active_duration",
)  # fmt: skip
IF_ATTRS = (
    "bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol", "bNumEndpoints",
    "bAlternateSetting", "interface", "supports_autosuspend", "authorized",
)  # fmt: skip
HUBPORT_ATTRS = ("connect_type", "state", "over_current_count", "early_stop", "quirks", "location", "usb3_lpm_permit")
TYPEC_PORT_ATTRS = (
    "data_role", "power_role", "power_operation_mode", "orientation", "preferred_role",
    "usb_typec_revision", "usb_power_delivery_revision", "vconn_source", "port_type",
    "supported_accessory_modes", "usb_capability", "usb_mode",
)  # fmt: skip
ALTMODE_ATTRS = ("svid", "mode", "vdo", "active", "description")
PSY_RX = re.compile(r"usb|ucsi|battmgr|ac|bat|wls|charger", re.I)
IRQ_RX = re.compile(r"usb|dwc|xhci|typec|glink|ucsi|phy|eusb|battmgr|pmic", re.I)


def selected(ctx: Ctx) -> list[str]:
    return list(PORTS) if ctx.port == "all" else [ctx.port]


def _find_controller(ctx: Ctx, ctrl: str) -> str | None:
    for pat in (f"/sys/devices/platform/soc@0/{ctrl}", f"/sys/devices/platform/*/{ctrl}", f"/sys/bus/platform/devices/{ctrl}"):
        g = ctx.glob(pat)
        if g:
            return ctx.realpath(g[0])
    return None


def port_info(ctx: Ctx, name: str) -> dict:
    """Resolves a port name to its controller, xHCI, buses, typec port, PHYs
    and repeaters, from the live sysfs links."""

    def build():
        ctrl = PORTS[name]["controller"]
        cdir = _find_controller(ctx, ctrl)
        info = {"name": name, "controller": ctrl, "controller_dir": cdir, "desc": PORTS[name]["desc"]}
        if not cdir:
            return info
        xh = [x for x in ctx.listdir(cdir) if x.startswith("xhci-hcd")]
        info["xhci"] = xh[0] if xh else None
        info["xhci_dir"] = f"{cdir}/{xh[0]}" if xh else None
        buses = []
        if info["xhci_dir"]:
            for u in ctx.listdir(info["xhci_dir"]):
                m = re.fullmatch(r"usb(\d+)", u)
                if m:
                    buses.append(int(m.group(1)))
        info["buses"] = sorted(buses)
        # PHY providers: platform suppliers (eUSB2) and phy-class suppliers
        # (the combo PHY is only linked as supplier:phy:phy-fd5000.phy.0).
        phys = {x.split(":", 2)[2] for x in ctx.listdir(cdir) if x.startswith("supplier:platform:") and x.endswith(".phy")}
        for x in ctx.listdir(cdir):
            if x.startswith("supplier:phy:"):
                real = ctx.realpath(f"/sys/class/phy/{x.split(':', 2)[2]}")
                phys.add(os.path.basename(os.path.dirname(os.path.dirname(real))))
        info["phys"] = sorted(phys)
        info["typec_port"] = None
        for p in ctx.glob("/sys/class/typec/port*"):
            b = os.path.basename(p)
            if re.fullmatch(r"port\d+", b) and ctx.exists(f"{p}/supplier:platform:{ctrl}"):
                info["typec_port"] = b
        reps = []
        for ph in ctx.glob("/sys/class/phy/*"):
            consumers = [x.split(":", 2)[2] for x in ctx.listdir(ph) if x.startswith("consumer:platform:")]
            if any(c in info["phys"] for c in consumers):
                real = ctx.realpath(ph)
                reps.append(os.path.basename(os.path.dirname(os.path.dirname(real))))
        info["repeaters"] = sorted(set(reps))
        return info

    return ctx.memo(("port", name), build)


def _devices_on(ctx: Ctx, buses: list[int]) -> list[str]:
    out = []
    for d in ctx.listdir("/sys/bus/usb/devices"):
        m = re.fullmatch(r"usb(\d+)|(\d+)-[\d.]+", d)
        if m and int(m.group(1) or m.group(2)) in buses:
            out.append(d)
    return sorted(out, key=lambda x: (x.startswith("usb") is False, x))


def _owned_by(ctx: Ctx, path: str, cdir: str | None) -> bool:
    return bool(cdir) and (ctx.realpath(path) + "/").startswith(cdir + "/")


# --- collectors ------------------------------------------------------------------
@collector("usb")
def ports(ctx: Ctx) -> dict:
    """How each port name resolves (controller, xHCI, buses, typec port, PHYs, repeaters)."""
    return {p: {k: v for k, v in port_info(ctx, p).items() if not k.endswith("_dir")} for p in selected(ctx)}


def _altmodes(ctx: Ctx, base: str, prefix: str) -> dict:
    out = {}
    for a in ctx.listdir(base):
        if re.fullmatch(re.escape(prefix) + r"\.\d+", a):
            out[a] = ctx.attrs(f"{base}/{a}", ALTMODE_ATTRS)
    return out


def _pd(ctx: Ctx, d: str) -> dict | None:
    """usb_power_delivery capabilities (source/sink PDOs) behind a port or
    partner's usb_power_delivery link."""
    if not ctx.exists(f"{d}/usb_power_delivery"):
        return None
    pd = ctx.realpath(f"{d}/usb_power_delivery")
    out = {"name": os.path.basename(pd), "revision": ctx.read(f"{pd}/revision")}
    for caps in ("source-capabilities", "sink-capabilities"):
        if ctx.isdir(f"{pd}/{caps}"):
            out[caps] = ctx.tree(f"{pd}/{caps}", depth=2)
    return out


@collector("usb")
def typec(ctx: Ctx) -> dict:
    """Type-C class: roles, power_operation_mode, orientation, partner identity,
    altmodes (port + partner, active), PD capabilities, cable."""
    out = {}
    lines = []
    for p in selected(ctx):
        tp = port_info(ctx, p).get("typec_port")
        if not tp:
            out[p] = {"typec_port": None}
            continue
        d = f"/sys/class/typec/{tp}"
        a = ctx.attrs(d, TYPEC_PORT_ATTRS)
        port = {k: parse.bracketed(v) if k in ("data_role", "power_role", "preferred_role", "port_type", "usb_mode") else v for k, v in a.items()}
        port["choices"] = {k: v for k, v in a.items() if "[" in (v or "")}
        port["altmodes"] = _altmodes(ctx, d, tp)
        port["pd"] = _pd(ctx, d)
        port["runtime_status"] = ctx.runtime_status(d)
        res = {"typec_port": tp, "port": port}
        pdir = f"{d}/{tp}-partner"
        if ctx.isdir(pdir):
            partner = ctx.attrs(pdir, ("accessory_mode", "supports_usb_power_delivery", "usb_power_delivery_revision", "number_of_alternate_modes", "type", "usb_mode"))
            if ctx.isdir(f"{pdir}/identity"):
                partner["identity"] = ctx.tree(f"{pdir}/identity", depth=0)
            partner["altmodes"] = _altmodes(ctx, pdir, f"{tp}-partner")
            partner["pd"] = _pd(ctx, pdir)
            res["partner"] = partner
        else:
            res["partner"] = None
        cdir = f"{d}/{tp}-cable"
        if ctx.isdir(cdir):
            cable = ctx.attrs(cdir, ("type", "plug_type", "usb_power_delivery_revision"))
            if ctx.isdir(f"{cdir}/identity"):
                cable["identity"] = ctx.tree(f"{cdir}/identity", depth=0)
            for plug in ctx.listdir(d):
                if plug.startswith(f"{tp}-plug"):
                    cable[plug] = {"altmodes": _altmodes(ctx, f"{d}/{plug}", plug)}
            res["cable"] = cable
        else:
            res["cable"] = None
        out[p] = res
        lines.append(f"== {p} {tp}")
        lines += [f"{k}={v}" for k, v in a.items()]
        lines.append(f"partner={res['partner']}")
        lines.append(f"cable={res['cable']}")
    ctx.raw("typec", "\n".join(lines))
    roles = {}
    for r in ctx.glob("/sys/class/usb_role/*"):
        roles[os.path.basename(r)] = ctx.read(f"{r}/role")
    out["_usb_role"] = roles
    return out


@collector("usb")
def ucsi(ctx: Ctx) -> dict:
    """UCSI debugfs (read-only files only: response, peak/avg current, VBUS voltage)."""
    out = {}
    for d in ctx.glob("/sys/kernel/debug/usb/ucsi/*"):
        out[os.path.basename(d)] = ctx.attrs(d, ("response", "peak_current", "avg_current", "vbus_voltage"))
    if not out:
        return {"present": False}
    return out


@collector("usb")
def power(ctx: Ctx) -> dict:
    """power_supply class (all supplies), plus the regulators and power domains
    feeding the port's controller, PHYs and repeaters."""
    sup = {}
    for d in ctx.glob("/sys/class/power_supply/*"):
        ue = parse.uevent(ctx.read(f"{d}/uevent") or "")
        sup[os.path.basename(d)] = psy_props(ue)
    out: dict = {"supplies": sup}
    rs = ctx.read("/sys/kernel/debug/regulator/regulator_summary")
    gs = ctx.read("/sys/kernel/debug/pm_genpd/pm_genpd_summary")
    regs = parse.regulator_summary(rs or "")
    genpd = parse.genpd_summary(gs or "")
    per = {}
    for p in selected(ctx):
        pi = port_info(ctx, p)
        names = [pi["controller"], *pi.get("phys", []), *pi.get("repeaters", [])]
        rx = "^(" + "|".join(re.escape(n) for n in names) + r")\b" if names else r"^$"
        r = parse.regulators_for(regs, rx)
        g = {k: v for k, v in genpd.items() if any(re.search(rx, dev.split(":")[-1]) for dev in v["devices"])}
        per[p] = {"regulators": r, "genpd": g}
    out["ports"] = per
    out["usb_genpd"] = {k: v["status"] for k, v in genpd.items() if "usb" in k}
    return out


def psy_props(ue: dict) -> dict:
    return {k.removeprefix("POWER_SUPPLY_").lower(): v for k, v in ue.items() if k.startswith("POWER_SUPPLY_")}


def _hub_ports(ctx: Ctx, ifdir: str) -> dict:
    out = {}
    for x in ctx.listdir(ifdir):
        if "-port" in x:
            pd = f"{ifdir}/{x}"
            a = ctx.attrs(pd, HUBPORT_ATTRS)
            a["peer"] = ctx.link_name(f"{pd}/peer")
            a["connector"] = ctx.link_name(f"{pd}/connector")
            a["runtime_status"] = ctx.runtime_status(pd)
            out[x] = a
    return out


def usb_device(ctx: Ctx, name: str) -> dict:
    d = f"/sys/bus/usb/devices/{name}"
    dev = ctx.attrs(d, DEV_ATTRS)
    dev["power"] = ctx.attrs(f"{d}/power", DEV_POWER_ATTRS)
    dev["driver"] = ctx.link_name(f"{d}/driver")
    ifs = {}
    for x in ctx.listdir(d):
        if re.fullmatch(rf"{re.escape(name if not name.startswith('usb') else name[3:] + '-0')}:\d+\.\d+", x):
            i = ctx.attrs(f"{d}/{x}", IF_ATTRS)
            i["driver"] = ctx.link_name(f"{d}/{x}/driver")
            hp = _hub_ports(ctx, f"{d}/{x}")
            if hp:
                i["ports"] = hp
            nets = ctx.listdir(f"{d}/{x}/net")
            if nets:
                i["net"] = nets
            ifs[x] = i
    dev["interfaces"] = ifs
    return dev


@collector("usb")
def tree(ctx: Ctx) -> dict:
    """lsusb -t, every device on the port's buses (speed, lanes, version,
    bMaxPower, config, strings, drivers, runtime PM, LPM), root hub ports, and
    `lsusb -v` of devices that are already runtime-active."""
    r = ctx.run(["lsusb", "-t"], raw="lsusb-t")
    lt = parse.lsusb_tree(r.out) if r.ok else {"error": r.summary()}
    out: dict = {"_lsusb_t": lt}
    for p in selected(ctx):
        pi = port_info(ctx, p)
        cdir = pi.get("controller_dir")
        devs = {}
        lsv = {}
        for name in _devices_on(ctx, pi.get("buses", [])):
            dv = usb_device(ctx, name)
            devs[name] = dv
            rs = dv["power"].get("runtime_status")
            busnum, devnum = dv.get("busnum"), dv.get("devnum")
            if not (busnum and devnum):
                continue
            ctrl_ok = ctx.runtime_status(cdir) == "active" if cdir else False
            if rs == "active" and ctrl_ok:
                v = ctx.run(["lsusb", "-v", "-s", f"{busnum}:{devnum}"], raw=f"lsusb-v_{p}_{name}")
                lsv[name] = "ok" if v.ok else v.summary()
            else:
                why = f"skipped: runtime_status={rs}" + ("" if ctrl_ok else ", controller not active")
                lsv[name] = why
                ctx.skipped.append({"what": f"lsusb -v {name}", "reason": why})
        out[p] = {"devices": devs, "lsusb_v": lsv}
    return out


@collector("usb")
def controller(ctx: Ctx) -> dict:
    """dwc3 controller and xHCI: runtime PM, driver, role switch; register-backed
    debugfs (dwc3 regdump key registers, xHCI PORTSC) only when runtime-active."""
    out = {}
    for p in selected(ctx):
        pi = port_info(ctx, p)
        cdir, xdir = pi.get("controller_dir"), pi.get("xhci_dir")
        if not cdir:
            out[p] = {"present": False}
            continue
        d: dict = {
            "driver": ctx.link_name(f"{cdir}/driver"),
            "runtime": ctx.attrs(f"{cdir}/power", ("runtime_status", "control", "runtime_usage", "runtime_active_kids", "wakeup")),
            "xhci": pi.get("xhci"),
        }
        if xdir:
            d["xhci_runtime"] = ctx.attrs(f"{xdir}/power", ("runtime_status", "control", "runtime_usage", "wakeup"))
        gate = [cdir] + ([xdir] if xdir else [])
        rd = ctx.read_regs(f"/sys/kernel/debug/usb/{pi['controller']}/regdump", gate)
        if isinstance(rd, str):
            ctx.raw(f"dwc3-regdump_{p}", rd)
            regs = parse.regdump(rd)
            key = {k: v for k, v in regs.items() if parse.DWC3_KEY_REGS.match(k)}
            d["dwc3_regs"] = key
        else:
            d["dwc3_regs"] = rd
        # dwc3 debugfs mode/link_state exist only in dual-role/gadget builds.
        for f in ("mode", "link_state"):
            path = f"/sys/kernel/debug/usb/{pi['controller']}/{f}"
            if ctx.exists(path):
                v = ctx.read_regs(path, gate)
                d[f"dwc3_{f}"] = v
        portsc = {}
        if pi.get("xhci"):
            base = f"/sys/kernel/debug/usb/xhci/{pi['xhci']}/ports"
            for pt in ctx.listdir(base):
                v = ctx.read_regs(f"{base}/{pt}/portsc", gate)
                portsc[pt] = parse.portsc(v) if isinstance(v, str) else v
        d["xhci_portsc"] = portsc
        out[p] = d
    return out


@collector("usb")
def phy(ctx: Ctx) -> dict:
    """PHYs and eUSB2 repeaters of the port: driver, compatible, runtime PM of
    the phy device and its provider. No PHY register reads."""
    out = {}
    for p in selected(ctx):
        pi = port_info(ctx, p)
        names = set(pi.get("phys", [])) | set(pi.get("repeaters", []))
        res = {}
        for ph in ctx.glob("/sys/class/phy/*"):
            real = ctx.realpath(ph)
            prov = os.path.dirname(os.path.dirname(real))
            pname = os.path.basename(prov)
            if pname not in names:
                continue
            compat = ctx.read_bytes(f"{prov}/of_node/compatible") or b""
            res[os.path.basename(ph)] = {
                "provider": pname,
                "role": "repeater" if pname in pi.get("repeaters", []) else "phy",
                "driver": ctx.link_name(f"{prov}/driver"),
                "compatible": [c for c in compat.decode(errors="replace").split("\0") if c],
                "phy_runtime_status": ctx.runtime_status(real),
                "provider_runtime_status": ctx.runtime_status(prov),
                "consumers": sorted(x.split(":", 2)[2] for x in ctx.listdir(real) if x.startswith("consumer:")),
            }
        out[p] = res
    return out


@collector("usb")
def irqs(ctx: Ctx) -> dict:
    """/proc/interrupts lines for usb/dwc3/xhci/typec/glink/ucsi/phy/pmic (counts summed over CPUs)."""
    t = ctx.read("/proc/interrupts") or ""
    irq = parse.interrupts(t)
    sel = {k: v for k, v in irq.items() if IRQ_RX.search(v.get("name", ""))}
    ctx.raw("interrupts-usb", "\n".join(l for l in t.splitlines()[1:] if IRQ_RX.search(l)))
    return {f"{k} {v.get('name', '')}".strip(): v["count"] for k, v in sel.items()}


def xhci_bus_map(ctx: Ctx) -> dict[str, list[int]]:
    m = {}
    for p in PORTS:
        pi = port_info(ctx, p)
        if pi.get("xhci"):
            m[pi["xhci"]] = pi.get("buses", [])
    return m


@collector("usb")
def klog(ctx: Ctx) -> dict:
    """Kernel log since boot: per-bus counts of -71/-110, resets, disconnects,
    enumeration failures, over-current, firmware load failures (cumulative,
    so diffs show what happened between snapshots); filtered lines in raw/."""
    recs = read_kmsg(ctx)
    msgs = [r["msg"] for r in recs]
    counts = parse.klog_counts(msgs, xhci_bus_map(ctx))
    buses = set()
    for p in selected(ctx):
        buses |= {str(b) for b in port_info(ctx, p).get("buses", [])}
    lines = [parse.fmt_kmsg(r) for r in recs if parse.FILTER_RX.search(r["msg"])]
    ctx.raw("klog-usb", "\n".join(lines))
    return {
        "records": len(recs),
        "total": counts["total"],
        "by_bus": {b: c for b, c in counts["by_bus"].items() if b in buses or ctx.port == "all"},
        "matching_lines": len(lines),
    }


@collector("usb")
def firmware(ctx: Ctx) -> dict:
    """Firmware load messages from this boot that name a device or driver on
    the port (e.g. a missing rtl8159-1.fw)."""
    recs = read_kmsg(ctx)
    out = {}
    fw_rx = re.compile(r"firmware|\.fw\b|\.bin\b|\.mbn\b", re.I)
    for p in selected(ctx):
        buses = port_info(ctx, p).get("buses", [])
        if not buses:
            out[p] = []
            continue
        # any device that was ever on the port's buses this boot: "r8152 6-1:1.0:"
        rx = re.compile(r"(?:^|\s)(?:" + "|".join(str(b) for b in buses) + r")-[\d.]+[:\s]")
        out[p] = [parse.fmt_kmsg(r) for r in recs if fw_rx.search(r["msg"]) and rx.search(r["msg"])][-40:]
    return out


@collector("usb")
def netdevs(ctx: Ctx) -> dict:
    """NICs on the port: carrier, operstate, speed/duplex, counters, ethtool
    (settings, -i, -S; only while the USB device is runtime-active) and addresses."""
    out = {}
    for p in selected(ctx):
        cdir = port_info(ctx, p).get("controller_dir")
        res = {}
        for n in ctx.glob("/sys/class/net/*"):
            if not _owned_by(ctx, f"{n}/device", cdir):
                continue
            ifname = os.path.basename(n)
            d = ctx.attrs(n, ("carrier", "operstate", "speed", "duplex", "mtu", "address", "carrier_changes", "carrier_up_count", "carrier_down_count"))
            d["stats"] = {k: ctx.read_int(f"{n}/statistics/{k}") for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets", "rx_errors", "tx_errors", "rx_dropped", "tx_dropped")}
            usbif = ctx.realpath(f"{n}/device")
            usbdev = os.path.dirname(usbif)
            rs = ctx.runtime_status(usbdev)
            d["usb_device"] = os.path.basename(usbdev)
            d["usb_runtime_status"] = rs
            d["driver"] = ctx.link_name(f"{n}/device/driver")
            if rs == "active":
                r = ctx.run(["ethtool", ifname], raw=f"ethtool_{ifname}")
                d["ethtool"] = parse.ethtool(r.out) if r.ok else r.summary()
                r = ctx.run(["ethtool", "-i", ifname], raw=f"ethtool-i_{ifname}")
                d["ethtool_i"] = parse.kv_lines(r.out, ":") if r.ok else r.summary()
                r = ctx.run(["ethtool", "-S", ifname], raw=f"ethtool-S_{ifname}")
                d["ethtool_S"] = parse.ethtool_stats(r.out) if r.ok else r.summary()
            else:
                d["ethtool"] = f"skipped: usb runtime_status={rs}"
            r = ctx.run(["ip", "-br", "addr", "show", "dev", ifname])
            d["addresses"] = r.out.split()[2:] if r.ok else r.summary()
            res[ifname] = d
        out[p] = res
    return out


@collector("usb")
def storage(ctx: Ctx) -> dict:
    """Disks on the port: UAS/usb-storage binding, block device, size, queue
    parameters, model; SMART only with --smart."""
    out = {}
    for p in selected(ctx):
        cdir = port_info(ctx, p).get("controller_dir")
        res = {}
        for b in ctx.glob("/sys/block/*"):
            if not ctx.exists(f"{b}/device") or not _owned_by(ctx, f"{b}/device", cdir):
                continue
            name = os.path.basename(b)
            real = ctx.realpath(f"{b}/device")
            usbif = next((seg for seg in real.split("/") if re.fullmatch(r"\d+-[\d.]+:\d+\.\d+", seg)), None)
            d = {
                "size_sectors": ctx.read_int(f"{b}/size"),
                "removable": ctx.read(f"{b}/removable"),
                "ro": ctx.read(f"{b}/ro"),
                "vendor": ctx.read(f"{b}/device/vendor"),
                "model": ctx.read(f"{b}/device/model"),
                "usb_interface": usbif,
                "transport": ctx.link_name(f"/sys/bus/usb/devices/{usbif}/driver") if usbif else None,
                "queue": ctx.attrs(f"{b}/queue", ("logical_block_size", "physical_block_size", "max_sectors_kb", "max_hw_sectors_kb", "rotational", "nr_requests", "scheduler", "write_cache")),
                "partitions": [x for x in ctx.listdir(b) if x.startswith(name)],
            }
            if ctx.smart:
                r = ctx.run(["smartctl", "-i", "-H", f"/dev/{name}"], raw=f"smartctl_{name}")
                d["smart"] = {"rc": r.rc, "health": next((l.split(":", 1)[1].strip() for l in r.out.splitlines() if "overall-health" in l or "Health Status" in l), None)}
            else:
                d["smart"] = "skipped: pass --smart"
            res[name] = d
        out[p] = res
    return out


def file_sha(ctx: Ctx, path: str) -> str | None:
    b = ctx.read_bytes(path, limit=64 << 20)
    return hashlib.sha256(b).hexdigest()[:16] if b is not None else None
