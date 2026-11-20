"""Pure parsers for a16-snap: text in, JSON-able data out. No I/O here, so
tests feed them recorded A16 output (tests/fixtures/snap/)."""

from __future__ import annotations

import re


def bracketed(v: str | None) -> str | None:
    """Type-C role files list the choices with the current one in brackets:
    '[host] device' -> 'host'. Plain values pass through."""
    if v is None:
        return None
    m = re.search(r"\[([^\]]+)\]", v)
    return m.group(1) if m else v.strip()


def kv_lines(text: str, sep: str = "=") -> dict:
    out = {}
    for line in (text or "").splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            out[k.strip()] = v.strip()
    return out


def uevent(text: str) -> dict:
    return kv_lines(text, "=")


# --- kernel log -----------------------------------------------------------------
def parse_kmsg_record(rec: str) -> dict | None:
    """One /dev/kmsg record 'pri,seq,ts_us,flags[,...];message' (continuation
    lines ' KEY=value' carry device properties)."""
    head, sep, rest = rec.partition(";")
    if not sep:
        return None
    f = head.split(",")
    try:
        pri, seq, ts = int(f[0]), int(f[1]), int(f[2])
    except (ValueError, IndexError):
        return None
    lines = rest.split("\n")
    msg = lines[0]
    props = {}
    for l in lines[1:]:
        if l.startswith(" ") and "=" in l:
            k, v = l[1:].split("=", 1)
            props[k] = v
    return {"seq": seq, "ts_us": ts, "level": pri & 7, "facility": pri >> 3, "msg": msg, "props": props}


def fmt_kmsg(r: dict) -> str:
    s, us = divmod(r["ts_us"], 1_000_000)
    return f"[{s:5d}.{us:06d}] {r['msg']}"


# Patterns counted per bus over the whole boot (cumulative, so a diff between
# two snapshots shows how many happened in between).
KLOG_PATTERNS = {
    "err_71": r"error -71\b|\s-71\b",
    "err_110": r"error -110\b|\s-110\b",
    "reset": r"\breset (?:\S+ )?(?:speed )?(?:USB|SuperSpeed|high-speed|full-speed|low-speed)|\breset SuperSpeed",
    "new_device": r"\bnew \S+ USB device\b|\bnew \S+ Gen \S+ USB device\b",
    "disconnect": r"\bUSB disconnect\b",
    "unable_to_enumerate": r"unable to enumerate",
    "cannot_reset": r"cannot reset",
    "cannot_disable": r"cannot disable",
    "over_current": r"over-?current",
    "not_accepting_address": r"not accepting address",
    "descriptor_read_error": r"device descriptor read",
    "cfgselector_reset": r"cfgselector.*\breset\b",
    "tx_status_error": r"Tx status",
    "firmware_missing": r"Direct firmware load .* failed|unable to load firmware|firmware: failed to load",
}
KLOG_RX = {k: re.compile(v, re.I) for k, v in KLOG_PATTERNS.items()}
# usb 5-1: / usb usb5-port1: / r8152-cfgselector 6-1: / r8152 6-1:1.0 eth0: / usb 5-1.2:
BUS_RX = re.compile(r"(?:^|\s)(?:usb(\d+)(?:-port\d+)?|(\d+)-[\d.]+(?::\d+\.\d+)?)[:\s]")
XHCI_RX = re.compile(r"\bxhci-hcd (xhci-hcd\.\d+\.auto)")
FILTER_RX = re.compile(
    r"usb|xhci|dwc3|typec|ucsi|pmic_glink|eusb2|repeater|qmp|phy|r8152|r8169|cdc_|uas|usb-storage|"
    r"firmware|\.fw\b|-71|-110|over-?current|carrier|link (?:is )?(?:up|down)|enu\d|eth\d",
    re.I,
)


def klog_bus(msg: str, xhci_bus: dict[str, list[int]] | None = None) -> list[int]:
    m = BUS_RX.search(msg)
    if m:
        return [int(next(g for g in m.groups() if g))]
    m = XHCI_RX.search(msg)
    if m and xhci_bus and m.group(1) in xhci_bus:
        return list(xhci_bus[m.group(1)])
    return []


def klog_counts(msgs: list[str], xhci_bus: dict[str, list[int]] | None = None) -> dict:
    """{'total': {pattern: n}, 'by_bus': {'5': {pattern: n}}} over the messages."""
    total: dict[str, int] = {k: 0 for k in KLOG_PATTERNS}
    by_bus: dict[str, dict[str, int]] = {}
    for msg in msgs:
        hits = [k for k, rx in KLOG_RX.items() if rx.search(msg)]
        if not hits:
            continue
        buses = klog_bus(msg, xhci_bus) or ["?"]
        for k in hits:
            total[k] += 1
            for b in buses:
                d = by_bus.setdefault(str(b), {})
                d[k] = d.get(k, 0) + 1
    return {"total": total, "by_bus": by_bus}


# --- /proc/interrupts -------------------------------------------------------------
def interrupts(text: str) -> dict:
    """{irq: {'count': total over CPUs, 'chip': ..., 'hwirq': ..., 'type': ..., 'name': ...}}"""
    lines = (text or "").splitlines()
    if not lines:
        return {}
    ncpu = len(lines[0].split())
    out = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        irq, rest = line.split(":", 1)
        irq = irq.strip()
        f = rest.split()
        counts = []
        for x in f[:ncpu]:
            if x.isdigit():
                counts.append(int(x))
            else:
                break
        tail = f[len(counts) :]
        d = {"count": sum(counts)}
        if irq.isdigit() and len(tail) >= 3:
            d["chip"], d["hwirq"] = tail[0], tail[1]
            d["type"] = tail[2] if tail[2] in ("Level", "Edge") else None
            d["name"] = " ".join(tail[3:] if d["type"] else tail[2:])
            if d["type"] is None:
                del d["type"]
        else:
            d["name"] = " ".join(tail)
        out[irq] = d
    return out


# --- regulator_summary -----------------------------------------------------------
def regulator_summary(text: str) -> dict:
    """{regulator: {'use', 'open', 'bypass', 'opmode', 'voltage_mv', 'current_ma', 'min_mv', 'max_mv',
    'consumers': {name: {'load_ma', 'min_mv', 'max_mv'}}, 'parent': name|None}}"""
    regs: dict = {}
    stack: list[tuple[int, str]] = []  # (indent, regulator)
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith(("regulator ", "---")):
            continue
        indent = len(line) - len(line.lstrip(" "))
        f = line.split()
        # A regulator line has use/open/bypass ints then opmode; a consumer
        # line has a load and min/max only.
        if len(f) >= 9 and f[1].isdigit() and f[2].isdigit() and f[3].isdigit():
            while stack and stack[-1][0] >= indent:
                stack.pop()
            name = f[0]
            regs[name] = {
                "use": int(f[1]),
                "open": int(f[2]),
                "bypass": int(f[3]),
                "opmode": f[4],
                "voltage_mv": _mv(f[5]),
                "current_ma": _mv(f[6]),
                "min_mv": _mv(f[7]),
                "max_mv": _mv(f[8]),
                "parent": stack[-1][1] if stack else None,
                "consumers": {},
            }
            stack.append((indent, name))
        elif len(f) >= 4 and stack:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if not stack:
                continue
            # consumer names may repeat (two handles); keep a count suffix.
            owner = regs[stack[-1][1]]["consumers"]
            name = f[0]
            key, i = name, 2
            while key in owner:
                key, i = f"{name}#{i}", i + 1
            vals = [x for x in f[1:] if re.match(r"^-?\d+m[AV]$", x)]
            owner[key] = {
                "enabled": int(f[1]) if f[1].isdigit() else None,
                "load_ma": _mv(vals[0]) if vals else None,
                "min_mv": _mv(vals[1]) if len(vals) > 1 else None,
                "max_mv": _mv(vals[2]) if len(vals) > 2 else None,
            }
    return regs


def _mv(s: str):
    m = re.match(r"^(-?\d+)m[AV]$", s)
    return int(m.group(1)) if m else s


def regulators_for(regs: dict, consumer_rx: str) -> dict:
    """The regulators that supply a consumer matching consumer_rx (with all
    their consumers), and their supply chain (names and voltages only).
    regulator-dummy (the stand-in for absent supplies) is left out."""
    rx = re.compile(consumer_rx)
    out: dict = {}
    for name, r in regs.items():
        if name == "regulator-dummy":
            continue
        if rx.search(name) or any(rx.search(c) for c in r["consumers"]):
            out[name] = r
    for name in list(out):
        n = out[name].get("parent")
        while n and n != "regulator-dummy" and n not in out and n in regs:
            out[n] = {k: v for k, v in regs[n].items() if k != "consumers"}
            n = regs[n].get("parent")
    return {k: out[k] for k in regs if k in out}


# --- pm_genpd_summary ------------------------------------------------------------
def genpd_summary(text: str) -> dict:
    """{domain: {'status': 'on'|'off-0', 'devices': {dev: runtime_status}}}"""
    out: dict = {}
    cur = None
    for line in (text or "").splitlines():
        if not line.strip() or line.startswith(("domain", "---")) or line.lstrip().startswith("/device"):
            continue
        if not line.startswith(" "):
            f = line.split()
            cur = f[0]
            out[cur] = {"status": f[1] if len(f) > 1 else None, "devices": {}}
        elif cur:
            f = line.split()
            if len(f) >= 2 and not line.startswith(" " * 20):
                out[cur]["devices"][f[0]] = f[1]
    return out


# --- debugfs register dumps -------------------------------------------------------
def regdump(text: str) -> dict:
    """'NAME = 0x...' lines -> {NAME: '0x...'}"""
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\S+)\s*=\s*(0x[0-9a-fA-F]+)", line)
        if m:
            out[m.group(1)] = m.group(2).lower()
    return out


DWC3_KEY_REGS = re.compile(r"^(GCTL|GSTS|GSNPSID|GUCTL\d?|GDBGLTSSM|GDBGLNMCC|GDBGBMU|GDBGLSP|GDBGEPINFO\d?|GUSB2PHYCFG\(0\)|GUSB3PIPECTL\(0\)|GFLADJ|GPRTBIMAP\w*|GEVNTCOUNT\(0\))$")


def portsc(text: str) -> dict:
    """xHCI debugfs portsc: '0x000002a0 Speed=0 Link=RxDetect PP ...'"""
    t = (text or "").strip()
    f = t.split()
    if not f:
        return {}
    d: dict = {"raw": f[0]}
    flags = []
    for x in f[1:]:
        if "=" in x:
            k, v = x.split("=", 1)
            d[k.lower()] = v
        else:
            flags.append(x)
    d["flags"] = " ".join(flags)
    return d


# --- lsusb -t ------------------------------------------------------------------
def lsusb_tree(text: str) -> dict:
    """{bus: {'driver','speed','ports', 'children': [{'port','dev','if','class','driver','speed','depth'}]}}"""
    buses: dict = {}
    cur = None
    for line in (text or "").splitlines():
        m = re.match(r"^/:\s+Bus (\d+)\.Port (\d+): Dev (\d+), Class=([^,]+), Driver=([^,]*), (\S+)", line)
        if m:
            cur = str(int(m.group(1)))
            drv = m.group(5)
            buses[cur] = {"driver": drv.split("/")[0], "ports": drv.split("/")[1] if "/" in drv else None, "speed": m.group(6), "children": []}
            continue
        m = re.match(r"^(\s*)\|__ Port (\d+): Dev (\d+), If (\d+), Class=([^,]+), Driver=([^,]*), (\S+)", line)
        if m and cur:
            buses[cur]["children"].append(
                {
                    "depth": len(m.group(1)) // 4,
                    "port": int(m.group(2)),
                    "dev": int(m.group(3)),
                    "if": int(m.group(4)),
                    "class": m.group(5),
                    "driver": m.group(6) or None,
                    "speed": m.group(7),
                }
            )
    return buses


# --- ethtool -------------------------------------------------------------------
def ethtool(text: str) -> dict:
    """`ethtool IF` -> {'Speed': '5000Mb/s', 'Link detected': 'yes', ...}
    (multi-line values such as the link-mode lists are joined)."""
    out: dict = {}
    key = None
    started = False
    for line in (text or "").splitlines():
        if line.startswith("Settings for "):
            started = True
            continue
        if not started or not line.strip():
            continue
        exp = line.expandtabs(8)
        indent = len(exp) - len(exp.lstrip(" "))
        if indent <= 8 and ":" in line:
            k, v = line.strip().split(":", 1)
            key = k.strip()
            out[key] = v.strip()
        elif key:
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def ethtool_stats(text: str) -> dict:
    out = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s+([^:]+):\s+(-?\d+)\s*$", line)
        if m:
            out[m.group(1).strip()] = int(m.group(2))
    return out


# --- power / suspend -----------------------------------------------------------
def qcom_stats(text: str) -> dict:
    """'Count: 4\\nLast Entered At: ...' -> {'count': 4, 'last_entered_at': ..., ...}"""
    out = {}
    for line in (text or "").splitlines():
        if ":" in line and not line.startswith("DDR"):
            k, v = line.split(":", 1)
            k = re.sub(r"\W+", "_", k.strip().lower()).strip("_")
            v = v.strip()
            out[k] = int(v) if re.fullmatch(r"-?\d+", v) else v
    return out


def ddr_stats(text: str) -> dict:
    out: dict = {"lpm": {}, "freq": {}}
    for line in (text or "").splitlines():
        m = re.match(r"DDR LPM Stat Name:(\S+)\s+count:(\d+)\s+Duration \(ticks\):(\d+)", line)
        if m:
            out["lpm"][m.group(1)] = {"count": int(m.group(2)), "ticks": int(m.group(3))}
            continue
        m = re.match(r"DDR Freq (\S+):\s+CP IDX:(\d+)\s+count:(\d+)\s+Duration \(ticks\):(\d+)", line)
        if m:
            out["freq"][m.group(1)] = {"cp_idx": int(m.group(2)), "count": int(m.group(3)), "ticks": int(m.group(4))}
    return out


def wakeup_sources(text: str, nonzero_only: bool = True) -> dict:
    lines = (text or "").splitlines()
    if not lines:
        return {}
    cols = lines[0].split()
    out = {}
    for line in lines[1:]:
        f = line.split()
        if len(f) < len(cols):
            continue
        # the name may contain spaces; the last len(cols)-1 fields are numbers
        nums = f[-(len(cols) - 1) :]
        name = " ".join(f[: len(f) - len(nums)])
        try:
            d = {c: int(v) for c, v in zip(cols[1:], nums)}
        except ValueError:
            continue
        if nonzero_only and not (d.get("event_count") or d.get("active_count") or d.get("wakeup_count")):
            continue
        out[name] = d
    return out


def dp_debug(text: str) -> dict:
    """msm dp_debug: section/key = value lines, sections flattened with '.'."""
    out = {}
    section = ""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            key = (section + "." if section else "") + k.strip().replace(" ", "_")
            v = v.strip()
            out[key] = int(v) if re.fullmatch(r"-?\d+", v) else v
        elif s.endswith(":"):
            section = s[:-1].strip()
        else:
            section = s
    return out


def iw_link(text: str) -> dict:
    out: dict = {}
    for line in (text or "").splitlines():
        if line.startswith("Connected to"):
            out["bssid"] = line.split()[2]
            out["connected"] = True
        elif line.startswith("Not connected"):
            out["connected"] = False
        elif ":" in line:
            k, v = line.strip().split(":", 1)
            out[k.strip()] = v.strip()
    return out


def amixer_contents(text: str) -> dict:
    """`amixer contents` -> {"numid=N,iface=..,name='...'": "values"}"""
    out: dict = {}
    key = None
    for line in (text or "").splitlines():
        if line.startswith("numid="):
            m = re.search(r"name='([^']*)'", line)
            key = m.group(1) if m else line
            out[key] = {}
        elif key and line.strip().startswith(": values="):
            out[key]["values"] = line.strip()[len(": values=") :]
        elif key and line.strip().startswith("; type="):
            out[key]["type"] = line.strip()[2:]
    return out
