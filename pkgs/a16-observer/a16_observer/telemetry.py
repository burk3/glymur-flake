"""`a16-telemetry <cmd>`: the A16 side of docs/observability-plan.md.

  heartbeat     1 Hz UDP heartbeat v2 to the observer host (boot_id, uptime, temps,
                battery, cpu freq, load, suspend count, state)
  mark-sleep    systemd-sleep hook body: state=suspending/resumed heartbeat,
                state file for the loop, fsync'd line in /var/lib/a16/sleep.log
  metrics       node_exporter textfile collector (qcom_stats residency,
                suspend_stats, wakeup sources, battery, boot identity, pstore,
                remoteprocs, RTC offset)
  boot-record   once per boot: identity + raw PMIC PON register dump (read-only,
                regmap debugfs) to /var/lib/a16/boots/<boot_id>.json and the journal
  netconsole    attach the configfs netconsole target to the observer host over the
                device that routes there (USB NIC by default), extended format,
                release prefix, userdata boot=<boot_id>; replay the ring buffer
  pstore-ship   log new /var/lib/systemd/pstore records to the journal
  redflags      follow /dev/kmsg and count kernel red flags (redflags.py) into a
                node_exporter textfile: a16_redflag_total{id,severity,subsystem}

Every path is under --root (default /), so the tests run on a synthetic tree.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time

from .journal import INFO, NOTICE, WARNING, Journal

XO_HZ = 19_200_000  # qcom_stats durations and timestamps are 19.2 MHz ticks
DEFAULT_TEMP_RE = r"^(cpu-[0-2]-[0-5]-0|gpuss-[01]|ddr-0|aoss-0)-thermal$"
STATE_FILE = "/run/a16-heartbeat/state"
RESUMED_HOLD = 10.0  # seconds state=resumed is reported before going back to run
PON_MESSAGE_ID = "6c0b6a1e2f0d4f7f9a3a16b0071e2c01"


def _p(root: str, path: str) -> str:
    return os.path.join(root, path.lstrip("/"))


def read(path: str, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return default


def read_int(path: str, default=None):
    v = read(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# --- heartbeat -----------------------------------------------------------------
def find_power_supplies(root: str) -> tuple[str | None, list[str]]:
    """The battery, and every external supply (Mains, USB: on the A16 the
    charger shows up as a UCSI USB supply, not as qcom-battmgr-ac)."""
    bat = None
    ext = []
    for d in sorted(glob.glob(_p(root, "/sys/class/power_supply/*"))):
        t = read(os.path.join(d, "type"))
        if t == "Battery" and bat is None:
            bat = d
        elif t in ("Mains", "USB"):
            ext.append(d)
    return bat, ext


def ac_online(ext: list[str]) -> int | None:
    vals = [v for v in (read_int(os.path.join(d, "online")) for d in ext) if v is not None]
    return max(vals) if vals else None


def find_temp_zones(root: str, pattern: str) -> list[str]:
    rx = re.compile(pattern)
    out = []
    for d in sorted(glob.glob(_p(root, "/sys/class/thermal/thermal_zone*"))):
        t = read(os.path.join(d, "type"), "")
        if rx.search(t):
            out.append(os.path.join(d, "temp"))
    return out


def read_state(path: str, now: float) -> str:
    v = read(path)
    if not v:
        return "run"
    parts = v.split()
    state = parts[0]
    try:
        t = float(parts[1])
    except (IndexError, ValueError):
        t = now
    if state == "resumed" and now - t > RESUMED_HOLD:
        return "run"
    return state


class HeartbeatSource:
    def __init__(self, root="/", temp_re=DEFAULT_TEMP_RE, state_file=STATE_FILE, interval=1.0, bat_every=5):
        self.root = root
        self.state_file = state_file
        self.interval = interval
        self.bat_every = max(1, bat_every)
        self.boot_id = read(_p(root, "/proc/sys/kernel/random/boot_id"), "00000000-0000-0000-0000-000000000000")
        self.zones = find_temp_zones(root, temp_re)
        self.bat, self.ac = find_power_supplies(root)
        self.bat_temp = None
        for d in glob.glob(_p(root, "/sys/class/thermal/thermal_zone*")):
            if read(os.path.join(d, "type")) == "qcom-battmgr-bat":
                self.bat_temp = os.path.join(d, "temp")
        self.policies = sorted(glob.glob(_p(root, "/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq")))
        self.tick = 0
        self.cached: dict[str, str] = {}

    def battery(self) -> dict[str, str]:
        # Battery reads go to the ADSP over glink: every bat_every ticks only.
        if self.tick % self.bat_every == 0 or not self.cached:
            c = {}
            if self.bat:
                p = read_int(os.path.join(self.bat, "power_now"))
                st = read(os.path.join(self.bat, "status"), "")
                if p is not None:
                    c["bat_uw"] = str(-abs(p) if st == "Discharging" else p)
                cap = read_int(os.path.join(self.bat, "capacity"))
                if cap is not None:
                    c["bat_pct"] = str(cap)
            on = ac_online(self.ac)
            if on is not None:
                c["ac"] = str(on)
            if self.bat_temp:
                t = read_int(self.bat_temp)
                if t is not None:
                    c["temp_bat_mc"] = str(t)
            self.cached = c
        return self.cached

    def payload(self, state: str | None = None, now: float | None = None) -> str:
        now = time.time() if now is None else now
        up = read(_p(self.root, "/proc/uptime"), "0 0").split()[0]
        f: dict[str, str] = {"v": "2", "iv": f"{self.interval:g}"}
        temps = [t for t in (read_int(z) for z in self.zones) if t is not None]
        if temps:
            f["temp_max_mc"] = str(max(temps))
        f.update(self.battery())
        freqs = [x for x in (read_int(p) for p in self.policies) if x is not None]
        if freqs:
            f["cpu_mhz_max"] = str(max(freqs) // 1000)
        la = read(_p(self.root, "/proc/loadavg"))
        if la:
            f["load1"] = la.split()[0]
        s = read_int(_p(self.root, "/sys/power/suspend_stats/success"))
        if s is not None:
            f["susp"] = str(s)
        f["state"] = state or read_state(self.state_file, now)
        self.tick += 1
        return f"{self.boot_id} {up} " + " ".join(f"{k}={v}" for k, v in f.items()) + "\n"


def parse_target(t: str) -> tuple[str, int]:
    host, _, port = t.rpartition(":")
    return host, int(port)


def send(sock: socket.socket, targets, data: bytes) -> int:
    ok = 0
    for t in targets:
        try:
            sock.sendto(data, t)
            ok += 1
        except OSError:
            pass
    return ok


def cmd_heartbeat(args) -> None:
    src = HeartbeatSource(args.root, args.temp_re, args.state_file, args.interval, args.battery_every)
    targets = [parse_target(t) for t in args.target]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        t0 = time.monotonic()
        send(sock, targets, src.payload().encode())
        time.sleep(max(0.05, args.interval - (time.monotonic() - t0)))


def cmd_mark_sleep(args) -> None:
    now = time.time()
    state = {"pre": "suspending", "post": "resumed"}.get(args.phase, args.phase)
    os.makedirs(os.path.dirname(args.state_file), exist_ok=True)
    with open(args.state_file + ".tmp", "w") as f:
        f.write(f"{state} {now:.3f}\n")
    os.replace(args.state_file + ".tmp", args.state_file)
    src = HeartbeatSource(args.root, args.temp_re, args.state_file, args.interval, 1)
    line = src.payload(state=state, now=now)
    targets = [parse_target(t) for t in args.target]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Twice: one datagram can be lost while the NIC is busy; the sink only
    # logs the state change once.
    send(sock, targets, line.encode())
    send(sock, targets, line.encode())
    if args.log:
        os.makedirs(os.path.dirname(args.log), exist_ok=True)
        fd = os.open(args.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
            os.write(fd, f"{stamp} {args.phase} {line}".encode())
            os.fsync(fd)
        finally:
            os.close(fd)


# --- textfile metrics ----------------------------------------------------------
def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(d: dict) -> str:
    if not d:
        return ""
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in d.items()) + "}"


class Metrics:
    def __init__(self):
        self.help: dict[str, tuple[str, str]] = {}
        self.samples: list[tuple[str, dict, float]] = []

    def add(self, name: str, value, labels=None, help_="", type_="gauge") -> None:
        if value is None:
            return
        self.help.setdefault(name, (help_, type_))
        self.samples.append((name, labels or {}, value))

    def text(self) -> str:
        out = []
        seen = set()
        for name, labels, value in self.samples:
            if name not in seen:
                h, t = self.help[name]
                if h:
                    out.append(f"# HELP {name} {h}")
                out.append(f"# TYPE {name} {t}")
                seen.add(name)
            out.append(f"{name}{_labels(labels)} {value:g}" if isinstance(value, float) else f"{name}{_labels(labels)} {value}")
        return "\n".join(out) + "\n"


def parse_qcom_stats(text: str) -> dict[str, int]:
    keys = {
        "Count": "count",
        "Last Entered At": "last_entered",
        "Last Exited At": "last_exited",
        "Accumulated Duration": "accumulated",
    }
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() in keys:
            try:
                out[keys[k.strip()]] = int(v.strip(), 0)
            except ValueError:
                pass
    return out


DDR_LPM_RE = re.compile(r"DDR LPM Stat Name:(\S+)\s+count:(\d+)\s+Duration \(ticks\):(\d+)")
DDR_FREQ_RE = re.compile(r"DDR Freq (\d+)Mhz:\s+CP IDX:(\d+)\s+count:(\d+)\s+Duration \(ticks\):(\d+)")


def parse_ddr_stats(text: str) -> tuple[list, list]:
    lpm = [(m.group(1), int(m.group(2)), int(m.group(3))) for m in DDR_LPM_RE.finditer(text)]
    freq = [(int(m.group(1)), int(m.group(3)), int(m.group(4))) for m in DDR_FREQ_RE.finditer(text)]
    return lpm, freq


def collect_qcom_stats(root: str, m: Metrics) -> None:
    base = _p(root, "/sys/kernel/debug/qcom_stats")
    for path in sorted(glob.glob(os.path.join(base, "*"))):
        name = os.path.basename(path)
        text = read(path)
        if text is None:
            continue
        if name == "ddr_stats":
            lpm, freq = parse_ddr_stats(text)
            for stat, count, ticks in lpm:
                m.add("a16_ddr_lpm_count", count, {"stat": stat}, "DDR low-power mode entries (qcom_stats ddr_stats)", "counter")
                m.add("a16_ddr_lpm_seconds", ticks / XO_HZ, {"stat": stat}, "Time in the DDR low-power mode", "counter")
            for mhz, count, ticks in freq:
                m.add("a16_ddr_freq_count", count, {"mhz": str(mhz)}, "DDR frequency switches into this level", "counter")
                m.add("a16_ddr_freq_seconds", ticks / XO_HZ, {"mhz": str(mhz)}, "Time at this DDR frequency", "counter")
            continue
        s = parse_qcom_stats(text)
        if not s:
            continue
        lab = {"subsystem": name}
        m.add("a16_qcom_stats_count", s.get("count"), lab, "Sleep-state entries (debugfs qcom_stats)", "counter")
        if "accumulated" in s:
            m.add("a16_qcom_stats_seconds", s["accumulated"] / XO_HZ, lab, "Accumulated time in the sleep state", "counter")
        if "last_entered" in s:
            m.add("a16_qcom_stats_last_entered_seconds", s["last_entered"] / XO_HZ, lab, "Last entry, XO time")
        if "last_exited" in s:
            m.add("a16_qcom_stats_last_exited_seconds", s["last_exited"] / XO_HZ, lab, "Last exit, XO time")


def collect_suspend(root: str, m: Metrics) -> None:
    base = _p(root, "/sys/power/suspend_stats")
    info = {}
    for path in sorted(glob.glob(os.path.join(base, "*"))):
        name = os.path.basename(path)
        v = read(path)
        if v is None:
            continue
        if name.startswith("last_"):
            info[name.removeprefix("last_failed_").removeprefix("last_")] = v
            continue
        try:
            m.add("a16_suspend_stats", int(v), {"stat": name}, "/sys/power/suspend_stats counters", "counter")
        except ValueError:
            pass
    if info:
        m.add("a16_suspend_last_failure_info", 1, info, "Last failed suspend (dev, errno, step)")
    irq = read_int(_p(root, "/sys/power/pm_wakeup_irq"))
    if irq is not None:
        m.add("a16_pm_wakeup_irq", irq, None, "IRQ that woke the last suspend (absent if none)")


def collect_wakeup(root: str, m: Metrics) -> None:
    for d in sorted(glob.glob(_p(root, "/sys/class/wakeup/wakeup*"))):
        name = read(os.path.join(d, "name"))
        if not name:
            continue
        lab = {"source": name}
        m.add("a16_wakeup_event_count", read_int(os.path.join(d, "event_count")), lab, "Wakeup source events", "counter")
        m.add("a16_wakeup_count", read_int(os.path.join(d, "wakeup_count")), lab, "Wakeup source aborted suspends", "counter")
        ms = read_int(os.path.join(d, "total_time_ms"))
        if ms is not None:
            m.add("a16_wakeup_active_seconds", ms / 1000, lab, "Wakeup source total active time", "counter")


def collect_battery(root: str, m: Metrics) -> None:
    bat, ac = find_power_supplies(root)
    if bat:
        st = read(os.path.join(bat, "status"), "")
        p = read_int(os.path.join(bat, "power_now"))
        if p is not None:
            m.add("a16_battery_power_watts", (-abs(p) if st == "Discharging" else p) / 1e6, None, "Battery power, negative when discharging")
        e = read_int(os.path.join(bat, "energy_now"))
        if e is not None:
            m.add("a16_battery_energy_wh", e / 1e6, None, "Battery energy now")
        ef = read_int(os.path.join(bat, "energy_full"))
        if ef is not None:
            m.add("a16_battery_energy_full_wh", ef / 1e6, None, "Battery energy when full")
        m.add("a16_battery_capacity_percent", read_int(os.path.join(bat, "capacity")), None, "Battery capacity")
        m.add("a16_battery_status_info", 1, {"status": st}, "Battery status")
    m.add("a16_ac_online", ac_online(ac), None, "Any external supply (Mains or USB) online")


ENTRY_COUNTER_RE = re.compile(r"\+(\d+)(?:-(\d+))?(\.conf)?$")


def efi_var(root: str, name: str) -> str | None:
    path = _p(root, f"/sys/firmware/efi/efivars/{name}-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return data[4:].decode("utf-16-le", "replace").rstrip("\0")


def boot_identity(root: str) -> dict:
    ident = {
        "boot_id": read(_p(root, "/proc/sys/kernel/random/boot_id"), ""),
        "kernel": read(_p(root, "/proc/sys/kernel/osrelease"), ""),
    }
    booted = os.path.realpath(_p(root, "/run/booted-system"))
    gen = ""
    for link in glob.glob(_p(root, "/nix/var/nix/profiles/system-*-link")):
        if os.path.realpath(link) == booted:
            mm = re.search(r"system-(\d+)-link$", link)
            if mm:
                gen = mm.group(1)
                break
    ident["generation"] = gen
    entry = efi_var(root, "LoaderEntrySelected") or ""
    ident["entry"] = entry
    mm = ENTRY_COUNTER_RE.search(entry)
    ident["tries_left"] = int(mm.group(1)) if mm else None
    if mm:
        base = entry[: mm.start()]
        ident["blessed"] = int(os.path.exists(_p(root, f"/boot/loader/entries/{base}.conf")))
    else:
        ident["blessed"] = 1 if entry else None
    return ident


def collect_boot(root: str, m: Metrics) -> None:
    ident = boot_identity(root)
    m.add(
        "a16_boot_info",
        1,
        {k: ident[k] for k in ("boot_id", "generation", "kernel", "entry")},
        "Running boot: boot_id, NixOS generation, kernel, systemd-boot entry",
    )
    m.add("a16_boot_count_tries_left", ident["tries_left"], None, "Boot counter tries left in the selected entry")
    m.add("a16_boot_blessed", ident["blessed"], None, "1 once the entry is blessed (counter removed)")
    btime = None
    for line in (read(_p(root, "/proc/stat"), "") or "").splitlines():
        if line.startswith("btime "):
            btime = int(line.split()[1])
    m.add("a16_boot_time_seconds", btime, None, "Boot time (epoch)")
    pstore = glob.glob(_p(root, "/var/lib/systemd/pstore/*"))
    m.add("a16_pstore_records", len(pstore), None, "Entries in /var/lib/systemd/pstore")
    live = glob.glob(_p(root, "/sys/fs/pstore/*"))
    m.add("a16_pstore_pending", len(live), None, "Records still in /sys/fs/pstore")


def collect_remoteproc(root: str, m: Metrics) -> None:
    for d in sorted(glob.glob(_p(root, "/sys/class/remoteproc/remoteproc*"))):
        name = read(os.path.join(d, "name"))
        st = read(os.path.join(d, "state"))
        if name and st:
            m.add("a16_remoteproc_running", int(st == "running"), {"name": name, "state": st}, "remoteproc state")


def collect_cpufreq(root: str, m: Metrics) -> None:
    # node_exporter's cpufreq collector is off on the A16 (it hangs on, or
    # fails to read, cpufreq/stats there); current and max per policy only.
    for d in sorted(glob.glob(_p(root, "/sys/devices/system/cpu/cpufreq/policy*"))):
        lab = {"policy": os.path.basename(d)}
        cur = read_int(os.path.join(d, "scaling_cur_freq"))
        if cur is not None:
            m.add("a16_cpufreq_hz", cur * 1000, lab, "Current frequency per cpufreq policy")
        mx = read_int(os.path.join(d, "scaling_max_freq"))
        if mx is not None:
            m.add("a16_cpufreq_max_hz", mx * 1000, lab, "Frequency limit per cpufreq policy")


def collect_rtc(root: str, m: Metrics, now: float) -> None:
    se = read_int(_p(root, "/sys/class/rtc/rtc0/since_epoch"))
    if se is not None:
        m.add("a16_rtc_offset_seconds", se - int(now), None, "rtc0 minus system clock (1 s resolution)")


def collect(root: str = "/", now: float | None = None) -> str:
    now = time.time() if now is None else now
    m = Metrics()
    t0 = time.monotonic()
    for fn in (
        collect_qcom_stats,
        collect_suspend,
        collect_wakeup,
        collect_battery,
        collect_boot,
        collect_remoteproc,
        collect_cpufreq,
    ):
        try:
            fn(root, m)
        except Exception as e:  # noqa: BLE001 - one broken source must not drop the rest
            print(f"a16-telemetry metrics: {fn.__name__}: {e}", file=sys.stderr)
            m.add("a16_textfile_errors", 1, {"collector": fn.__name__}, "Collector errors in this run")
    collect_rtc(root, m, now)
    m.add("a16_textfile_duration_seconds", round(time.monotonic() - t0, 4), None, "Time to collect")
    return m.text()


def cmd_metrics(args) -> None:
    text = collect(args.root)
    if args.out == "-":
        sys.stdout.write(text)
        return
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, args.out)


# --- kernel red flags -----------------------------------------------------------
# A long-running counter rather than part of `metrics`: it keeps /dev/kmsg open
# and reads only new records as they arrive (poll), so the cost is one regex
# prefilter per kernel message, not a re-scan of the ring buffer every 15 s.
# The counts are since boot; the state file (/run, so per boot) carries them and
# the kmsg cursor across restarts of the service.
def kmsg_file_records(path: str):
    """Records of a regular file holding /dev/kmsg-format text (tests):
    continuation lines start with a space."""
    rec = None
    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b" ") and rec is not None:
                rec += line
                continue
            if rec is not None:
                yield rec
            rec = line
    if rec is not None:
        yield rec


class RedflagCounter:
    def __init__(self, boot_id: str, state: dict | None = None):
        from . import redflags as rf

        self.rf = rf
        st = state if state and state.get("boot_id") == boot_id else {}
        self.boot_id = boot_id
        self.seq = st.get("seq", -1)
        self.counts: dict[str, int] = dict(st.get("counts", {}))
        self.lost = st.get("lost", 0)
        self.dirty = True

    def feed(self, rec: bytes) -> None:
        from .snap import parse

        r = parse.parse_kmsg_record(rec.decode("utf-8", "replace").rstrip("\n"))
        if r is None or r["seq"] <= self.seq:
            return
        if self.seq >= 0 and r["seq"] > self.seq + 1:
            self.lost += r["seq"] - self.seq - 1  # overwritten before we read them
        self.seq = r["seq"]
        p = self.rf.classify(r["msg"])
        if p is not None:
            self.counts[p.id] = self.counts.get(p.id, 0) + 1
            self.dirty = True

    def state(self) -> dict:
        return {"boot_id": self.boot_id, "seq": self.seq, "counts": self.counts, "lost": self.lost}

    def text(self) -> str:
        m = Metrics()
        for p in self.rf.PATTERNS:
            lab = {"id": p.id, "severity": p.sev, "subsystem": p.subsystem}
            m.add("a16_redflag_total", self.counts.get(p.id, 0), lab,
                  "Kernel log lines matching each red flag pattern since boot (redflags.py)", "counter")
        m.add("a16_redflag_kmsg_seq", self.seq, None, "Last kernel log sequence number read")
        m.add("a16_redflag_kmsg_lost_total", self.lost, None,
              "Kernel log records overwritten before the counter read them", "counter")
        return m.text()


def _write_atomic(path: str, text: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def cmd_redflags(args) -> int:
    import select
    import stat as stat_

    boot_id = read(_p(args.root, "/proc/sys/kernel/random/boot_id"), "?")
    state = None
    try:
        with open(args.state) as f:
            state = json.load(f)
    except (OSError, ValueError):
        pass
    c = RedflagCounter(boot_id, state)

    def flush():
        if args.out == "-":
            sys.stdout.write(c.text())
            sys.stdout.flush()
        else:
            _write_atomic(args.out, c.text())
            _write_atomic(args.state, json.dumps(c.state()), 0o600)
        c.dirty = False

    kmsg = _p(args.root, args.kmsg)
    if not stat_.S_ISCHR(os.stat(kmsg).st_mode):  # tests: a file of records
        for rec in kmsg_file_records(kmsg):
            c.feed(rec)
        flush()
        return 0
    fd = os.open(kmsg, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    last_flush = 0.0
    try:
        while True:
            try:
                rec = os.read(fd, 16384)
            except BlockingIOError:
                if args.once:
                    break
                now = time.monotonic()
                if c.dirty and now - last_flush >= args.min_interval:
                    flush()
                    last_flush = now
                # Block until new records; wake up to flush a pending change.
                select.select([fd], [], [], args.min_interval if c.dirty else None)
                continue
            except BrokenPipeError:  # overwritten while reading; next record
                continue
            if not rec:
                break
            c.feed(rec)
    finally:
        os.close(fd)
    flush()
    return 0


# --- boot record / PON ---------------------------------------------------------
def find_pon(root: str) -> list[dict]:
    """PON blocks from the live DT: [{"regmap": "0-00", "name": "hlos", "base": 0x1300}, ...]."""
    out = []
    for dev in sorted(glob.glob(_p(root, "/sys/bus/spmi/devices/*-*"))):
        of = os.path.join(dev, "of_node")
        for node in sorted(glob.glob(os.path.join(of, "pon@*"))):
            compat = read(os.path.join(node, "compatible"), "") or ""
            if "pon" not in compat:
                continue
            try:
                with open(os.path.join(node, "reg"), "rb") as f:
                    reg = f.read()
            except OSError:
                continue
            cells = [int.from_bytes(reg[i : i + 4], "big") for i in range(0, len(reg) - 3, 4)]
            try:
                with open(os.path.join(node, "reg-names"), "rb") as f:
                    names = [n.decode() for n in f.read().split(b"\0") if n]
            except OSError:
                names = []
            for i, base in enumerate(cells):
                out.append(
                    {
                        "regmap": os.path.basename(dev),
                        "name": names[i] if i < len(names) else f"reg{i}",
                        "base": base,
                        "compatible": compat.replace("\0", " ").strip(),
                    }
                )
    return out


LINE_RE = re.compile(r"^([0-9a-f]+): ([0-9a-f]+|X+)$")


def read_regmap(path: str, base: int, count: int = 0x100) -> bytes:
    """Read `count` registers from a regmap debugfs `registers` file, by seeking.

    Lines are fixed width ("%0Nx: %0Mx\\n"), so register r of a regmap with no
    unreadable holes is at r * line_len. The address in each line is checked;
    on a mismatch it falls back to a forward scan from the start.
    """
    with open(path, "rb", buffering=0) as f:
        first = f.read(64).split(b"\n")[0].decode()
        mm = LINE_RE.match(first)
        if not mm or int(mm.group(1), 16) != 0:
            raise ValueError(f"{path}: unexpected first line {first!r}")
        line_len = len(first) + 1
        f.seek(base * line_len)
        chunk = f.read(count * line_len).decode()
        out = _parse_regs(chunk, base, count)
        if out is not None:
            return out
        f.seek(0)
        regs = {}
        while True:
            block = f.read(1 << 16)
            if not block:
                break
            for line in block.decode().splitlines():
                m2 = LINE_RE.match(line)
                if m2:
                    a = int(m2.group(1), 16)
                    if base <= a < base + count:
                        regs[a] = int(m2.group(2), 16) if "X" not in m2.group(2) else 0
            if regs and max(regs) >= base + count - 1:
                break
        return bytes(regs.get(base + i, 0) for i in range(count))


def _parse_regs(chunk: str, base: int, count: int) -> bytes | None:
    vals = []
    for i, line in enumerate(chunk.splitlines()[:count]):
        mm = LINE_RE.match(line)
        if not mm or int(mm.group(1), 16) != base + i:
            return None
        vals.append(int(mm.group(2), 16) if "X" not in mm.group(2) else 0)
    if len(vals) != count:
        return None
    return bytes(vals)


def previous_boot_end() -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-b", "-1", "-n", "200", "-o", "cat", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return classify_end(out)


def previous_boot_id() -> str | None:
    """The boot before this one in the local journal (dashed, like /proc's)."""
    try:
        out = subprocess.run(
            ["journalctl", "--list-boots", "-o", "json", "--no-pager", "-q"], capture_output=True, text=True, timeout=30
        ).stdout
        boots = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    for b in boots:
        if b.get("index") == -1 and re.fullmatch(r"[0-9a-f]{32}", b.get("boot_id", "")):
            h = b["boot_id"]
            return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return None


def classify_end(tail: str) -> str:
    """How a boot ended, from the tail of its journal: reboot, poweroff,
    shutdown (some orderly shutdown, kind unknown), unclean or unknown."""
    if not tail.strip():
        return "unknown"
    if re.search(r"Reached target.*\bReboot\b|systemd-reboot\.service|reboot: Restarting", tail):
        return "reboot"
    if re.search(r"Reached target.*\bPower-Off\b|systemd-poweroff\.service|reboot: Power down", tail):
        return "poweroff"
    if re.search(r"systemd-shutdow|Sending SIGTERM to remaining processes|Reached target.*\b(Shutdown|System Shutdown|Final Step)\b", tail):
        return "shutdown"
    return "unclean"


def boot_record(root: str, journal_end: bool = True) -> dict:
    ident = boot_identity(root)
    rec = {"time": time.time(), **ident}
    if journal_end:
        rec["previous_end"] = previous_boot_end()
        rec["previous_boot_id"] = previous_boot_id()
    pons = []
    for p in find_pon(root):
        path = _p(root, f"/sys/kernel/debug/regmap/{p['regmap']}/registers")
        try:
            data = read_regmap(path, p["base"])
            pons.append({**p, "hex": data.hex()})
        except (OSError, ValueError) as e:
            pons.append({**p, "error": str(e)})
    rec["pon"] = pons
    return rec


def cmd_boot_record(args) -> None:
    rec = boot_record(args.root)
    os.makedirs(args.dir, exist_ok=True)
    path = os.path.join(args.dir, f"{rec['boot_id'] or 'unknown'}.json")
    with open(path + ".tmp", "w") as f:
        json.dump(rec, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + ".tmp", path)
    fields = {
        "MESSAGE_ID": PON_MESSAGE_ID,
        "A16_BOOT_ID": rec["boot_id"],
        "A16_GENERATION": rec["generation"],
        "A16_KERNEL": rec["kernel"],
        "A16_ENTRY": rec["entry"],
        "A16_PREVIOUS_END": rec.get("previous_end"),
        "A16_PREVIOUS_BOOT_ID": rec.get("previous_boot_id"),
    }
    summary = []
    for p in rec["pon"]:
        key = f"A16_PON_{p['name'].upper()}"
        if "hex" in p:
            fields[key] = p["hex"]
            fields[key + "_BASE"] = f"{p['regmap']}:{p['base']:#x}"
            summary.append(f"{p['name']}@{p['regmap']}:{p['base']:#x} {p['hex'][:32]}...")
        else:
            summary.append(f"{p['name']}: {p['error']}")
    Journal("a16-boot-record").send(
        f"boot {rec['boot_id']} gen {rec['generation'] or '?'} kernel {rec['kernel']} entry {rec['entry'] or '?'}"
        f" previous boot ended {rec.get('previous_end', '?')}; PON " + ("; ".join(summary) or "not found"),
        NOTICE,
        **fields,
    )
    print(path)


# --- netconsole sender -----------------------------------------------------------
NC_DIR = "/sys/kernel/config/netconsole"


def ip_json(*args: str) -> list:
    try:
        out = subprocess.run(["ip", "-j", *args], capture_output=True, text=True, timeout=10).stdout
        return json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def resolve_route(remote: str) -> dict | None:
    """{"dev", "src", "nexthop"} for packets to `remote`, or None."""
    r = ip_json("route", "get", remote)
    if not r or "dev" not in r[0]:
        return None
    return {"dev": r[0]["dev"], "src": r[0].get("prefsrc"), "nexthop": r[0].get("gateway", remote)}


def resolve_mac(ip: str, dev: str) -> str | None:
    """The next hop's MAC (netconsole sends raw frames; it would broadcast
    without one). One ping fills the neighbour table if needed."""

    def lookup():
        for n in ip_json("neigh", "show", ip, "dev", dev):
            if n.get("lladdr") and not set(n.get("state", [])) & {"FAILED", "INCOMPLETE"}:
                return n["lladdr"]
        return None

    mac = lookup()
    if mac is None:
        subprocess.run(["ping", "-c", "1", "-W", "1", "-I", dev, ip], capture_output=True, timeout=10)
        mac = lookup()
    return mac


def nc_want(route: dict, mac: str, remote: str, port: int, local_port: int) -> dict:
    return {
        "dev_name": route["dev"],
        "local_ip": route["src"] or "",
        "local_port": str(local_port),
        "remote_ip": remote,
        "remote_port": str(port),
        "remote_mac": mac.lower(),
    }


def nc_write(path: str, value: str) -> None:
    with open(path, "w") as f:
        f.write(value + "\n")


def nc_matches(t: str, want: dict, boot_id: str) -> bool:
    if read(os.path.join(t, "enabled")) != "1":
        return False
    for k, v in want.items():
        if (read(os.path.join(t, k)) or "").lower() != v.lower():
            return False
    if read(os.path.join(t, "extended")) != "1":
        return False
    if os.path.isdir(os.path.join(t, "userdata")):
        return read(os.path.join(t, "userdata", "boot", "value")) == boot_id
    return True


def nc_configure(t: str, want: dict, boot_id: str, release: bool) -> None:
    """(Re)configure the configfs target: attributes change only while it is
    disabled; extended format, the kernel release prefix and userdata boot=."""
    os.makedirs(t, exist_ok=True)
    if read(os.path.join(t, "enabled")) == "1":
        nc_write(os.path.join(t, "enabled"), "0")
    for k, v in want.items():
        if v:
            nc_write(os.path.join(t, k), v)
    nc_write(os.path.join(t, "extended"), "1")
    if release and os.path.exists(os.path.join(t, "release")):
        nc_write(os.path.join(t, "release"), "1")
    ud = os.path.join(t, "userdata")
    if os.path.isdir(ud):
        os.makedirs(os.path.join(ud, "boot"), exist_ok=True)
        nc_write(os.path.join(ud, "boot", "value"), boot_id)
    nc_write(os.path.join(t, "enabled"), "1")


def nc_disable(t: str) -> bool:
    if read(os.path.join(t, "enabled")) == "1":
        nc_write(os.path.join(t, "enabled"), "0")
        return True
    return False


def kmsg_records(path: str = "/dev/kmsg"):
    """Every record still in the kernel ring buffer, in /dev/kmsg's format
    ("prio,seq,usec,flags;text\n KEY=VALUE\n..."), which is netconsole's
    extended format too."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        while True:
            try:
                rec = os.read(fd, 16384)
            except BlockingIOError:
                return
            except BrokenPipeError:
                continue  # overwritten while reading: next one
            if not rec:
                return
            yield rec
    finally:
        os.close(fd)


def replay_payloads(records, release: str | None, boot_id: str):
    """Datagrams that look like the target's own: release prefix, the record,
    userdata boot=, plus replay=1 so the sink doesn't count gaps on them."""
    pre = (release + ",").encode() if release else b""
    tail = f" boot={boot_id}\n replay=1\n".encode()
    for rec in records:
        if not rec.endswith(b"\n"):
            rec += b"\n"
        yield pre + rec + tail


def nc_replay(src: str | None, remote: str, port: int, release: str | None, boot_id: str, kmsg: str, rate: float) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if src:
        try:
            sock.bind((src, 0))
        except OSError:
            pass
    n = 0
    for d in replay_payloads(kmsg_records(kmsg), release, boot_id):
        try:
            sock.sendto(d, (remote, port))
        except OSError:
            time.sleep(0.05)
            try:
                sock.sendto(d, (remote, port))
            except OSError:
                continue
        n += 1
        if rate > 0:
            time.sleep(1.0 / rate)
    sock.close()
    return n


def cmd_netconsole(args) -> int:
    """attach: point the configfs target at the observer host through whatever device
    routes there now, if that device is allowed (--dev-re), then replay the
    ring buffer. Idempotent: a target that already matches is left alone (no
    replay). On a disallowed or missing route the target is disabled.
    detach: disable it."""
    j = Journal("a16-netconsole")
    nc = _p(args.root, NC_DIR)
    t = os.path.join(nc, args.name)
    if args.action == "detach":
        if os.path.isdir(t) and nc_disable(t):
            j.send(f"netconsole target {args.name} disabled", NOTICE)
        return 0
    if not os.path.isdir(nc):
        j.send(f"{nc} missing (netconsole module not loaded or CONFIG_NETCONSOLE_DYNAMIC off)", WARNING)
        return 1
    remote, port = parse_target(args.target)
    route = resolve_route(remote)
    if route is None:
        if os.path.isdir(t) and nc_disable(t):
            j.send(f"no route to {remote}; netconsole target disabled", NOTICE)
        print(f"no route to {remote} yet")
        return 1  # the service retries
    if not re.fullmatch(args.dev_re, route["dev"]):
        if os.path.isdir(t) and nc_disable(t):
            j.send(f"route to {remote} is via {route['dev']}, not allowed (--dev-re {args.dev_re}); netconsole disabled", NOTICE)
        else:
            print(f"route to {remote} is via {route['dev']}; not attaching (--dev-re {args.dev_re})")
        return 0
    mac = resolve_mac(route["nexthop"], route["dev"])
    if mac is None:
        j.send(f"no MAC for next hop {route['nexthop']} on {route['dev']}; netconsole not attached", WARNING)
        return 1
    boot_id = read(_p(args.root, "/proc/sys/kernel/random/boot_id"), "")
    want = nc_want(route, mac, remote, port, args.local_port)
    if os.path.isdir(t) and nc_matches(t, want, boot_id):
        print(f"netconsole target {args.name} already on {route['dev']} -> {remote}:{port}")
        return 0
    try:
        nc_configure(t, want, boot_id, not args.no_release)
    except OSError as e:
        j.send(f"configuring netconsole target {t} failed: {e}", WARNING)
        return 1
    release = None if args.no_release else read(_p(args.root, "/proc/sys/kernel/osrelease"))
    msg = f"netconsole on {route['dev']} {route['src']} -> {remote}:{port} ({mac}), extended, boot={boot_id}"
    if args.replay:
        n = nc_replay(route["src"], remote, port, release, boot_id, _p(args.root, "/dev/kmsg"), args.rate)
        msg += f"; replayed {n} ring buffer records"
    j.send(msg, NOTICE, A16_NETCONSOLE_DEV=route["dev"])
    return 0


# --- pstore shipping ---------------------------------------------------------------
PSTORE_MESSAGE_ID = "b1e4c0a3d5f64e1f8a2716c3e0d9f5a7"
PSTORE_TEXT_MAX = 48 * 1024
# The summary line of a record: the first line matching the earliest of these.
KEY_RES = [
    re.compile(r"Kernel panic"),
    re.compile(r"Unable to handle|Internal error|BUG:|hard LOCKUP|soft lockup|hung_task|watchdog", re.I),
    re.compile(r"Oops|Call trace", re.I),
]


def pstore_text(rec_dir: str) -> tuple[str, list[str]]:
    """A record's kernel log: systemd-pstore's reassembled dmesg.txt files if
    present, else the raw dmesg-* parts, in name order."""
    files = sorted(glob.glob(os.path.join(rec_dir, "**", "dmesg.txt"), recursive=True))
    if not files:
        files = sorted(f for f in glob.glob(os.path.join(rec_dir, "**", "*"), recursive=True) if os.path.isfile(f))
    parts = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                parts.append(fh.read().decode("utf-8", "backslashreplace"))
        except OSError:
            continue
    return "".join(parts), [os.path.relpath(f, rec_dir) for f in files]


def shorten(text: str, limit: int = PSTORE_TEXT_MAX, lines: int = 50) -> str:
    if len(text.encode()) <= limit:
        return text
    ls = text.splitlines()
    cut = "\n".join(ls[:lines] + [f"[... {len(ls) - 2 * lines} lines cut ...]"] + ls[-lines:])
    return cut.encode()[:limit].decode("utf-8", "ignore")


def cmd_pstore_ship(args) -> int:
    """Log every pstore record not shipped yet to the journal (so the upload
    carries it to the observer host), then remember it in --state."""
    d = _p(args.root, args.dir)
    try:
        with open(args.state) as f:
            shipped = set(f.read().split())
    except OSError:
        shipped = set()
    j = Journal("a16-pstore")
    new = []
    for rec in sorted(glob.glob(os.path.join(d, "*"))):
        rid = os.path.basename(rec)
        if rid in shipped or not os.path.isdir(rec):
            continue
        text, files = pstore_text(rec)
        when = ""
        if rid.isdigit() and len(rid) >= 10:
            # EFI pstore ids are <epoch seconds><part><count>; systemd-pstore
            # names the directory after the first 10 digits.
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(rid[:10])))
        lines = text.splitlines()
        key = next((ln for rx in KEY_RES for ln in lines if rx.search(ln)), lines[-1] if lines else "(empty)")
        j.send(
            f"pstore record {rid}{' from ' + when if when else ''}: {len(text)} bytes; {key.strip()[:200]}",
            WARNING,
            MESSAGE_ID=PSTORE_MESSAGE_ID,
            A16_PSTORE_ID=rid,
            A16_PSTORE_TIME=when or None,
            A16_PSTORE_FILES=" ".join(files),
            A16_PSTORE_BYTES=len(text),
            A16_PSTORE_TEXT=shorten(text) if text else "",
        )
        new.append(rid)
    if new:
        os.makedirs(os.path.dirname(args.state) or ".", exist_ok=True)
        with open(args.state, "a") as f:
            f.write("".join(r + "\n" for r in new))
            f.flush()
            os.fsync(f.fileno())
    print(f"shipped {len(new)} pstore record(s)" + (": " + " ".join(new) if new else ""))
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="a16-telemetry", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="/")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def hb_args(p):
        p.add_argument("--target", action="append", default=[], help="HOST:PORT (repeatable)")
        p.add_argument("--interval", type=float, default=1.0)
        p.add_argument("--temp-re", default=DEFAULT_TEMP_RE)
        p.add_argument("--state-file", default=STATE_FILE)

    p = sub.add_parser("heartbeat")
    hb_args(p)
    p.add_argument("--battery-every", type=int, default=5)
    p = sub.add_parser("mark-sleep")
    hb_args(p)
    p.add_argument("phase", help="pre|post (systemd-sleep's $1)")
    p.add_argument("--log", default="/var/lib/a16/sleep.log")
    p = sub.add_parser("metrics")
    p.add_argument("--out", default="/run/a16-metrics/a16.prom")
    p = sub.add_parser("boot-record")
    p.add_argument("--dir", default="/var/lib/a16/boots")
    p = sub.add_parser("netconsole", help="attach/detach the configfs netconsole target")
    p.add_argument("action", choices=["attach", "detach"])
    p.add_argument("--target", required=True, help="REMOTE_IP:PORT")
    p.add_argument("--name", default="a16", help="configfs target name")
    p.add_argument("--dev-re", default="enu.*", help="devices netconsole may use (full match)")
    p.add_argument("--local-port", type=int, default=6665)
    p.add_argument("--no-release", action="store_true", help="don't prepend the kernel release")
    p.add_argument("--replay", action="store_true", help="send the ring buffer after (re)attaching")
    p.add_argument("--rate", type=float, default=2000, help="replay datagrams per second")
    p = sub.add_parser("redflags", help="count kernel red flags from /dev/kmsg into a textfile")
    p.add_argument("--out", default="/run/a16-metrics/redflags.prom", help="textfile ('-': stdout)")
    p.add_argument("--state", default="/run/a16-redflags/state.json", help="counts and kmsg cursor, this boot")
    p.add_argument("--kmsg", default="/dev/kmsg")
    p.add_argument("--once", action="store_true", help="read what the ring buffer holds, write, exit")
    p.add_argument("--min-interval", type=float, default=2.0, help="seconds between textfile rewrites")
    p = sub.add_parser("pstore-ship", help="log new pstore records to the journal")
    p.add_argument("--dir", default="/var/lib/systemd/pstore")
    p.add_argument("--state", default="/var/lib/a16/pstore-shipped")

    args = ap.parse_args(argv)
    rc = {
        "heartbeat": cmd_heartbeat,
        "mark-sleep": cmd_mark_sleep,
        "metrics": cmd_metrics,
        "boot-record": cmd_boot_record,
        "netconsole": cmd_netconsole,
        "pstore-ship": cmd_pstore_ship,
        "redflags": cmd_redflags,
    }[args.cmd](args)
    if rc:
        sys.exit(rc)


if __name__ == "__main__":
    main()
