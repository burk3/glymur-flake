"""a16: one place to look at the Zenbook A16 from the observer host.

Reads (no lease needed):
  a16 status [--json]            alive? heartbeat, both NICs, serial, metrics
  a16 log [-f] [--since 10m] [-g RE] [-k] [--source S,...]
                                 merged timeline: serial, heartbeat events,
                                 netconsole, marks, serial input; -k adds the
                                 A16 kernel log, --source journal the whole
                                 uploaded A16 journal. VictoriaLogs (LogsQL)
                                 when it answers, else journalctl; -f always
                                 follows the journal files
  a16 redflags [--since T|--boot [ID]] [--sev warn] [-l]
                                 known-bad kernel log lines (redflags.py) from the
                                 kernel log, netconsole and serial, deduplicated,
                                 grouped by pattern with counts and the last one;
                                 default: the current boot, warn and worse
  a16 serial tail [-f] [-n N]    serial lines from the journal
  a16 serial follow [--backlog]  live bytes from the serial daemon
  a16 serial status              the serial daemon's state
  a16 metrics [NAME|--query Q]   VictoriaMetrics (127.0.0.1:8428), shortcuts:
                                 up temps power residency wakeups hb boot redflags
  a16 metrics pause|resume|state toggle vmagent's A16 scrape (idle-power runs)
  a16 boots [-n 20] [--ref ID]   boots (heartbeat sink + the A16's boot records),
                                 how they ended, PON dump diff vs a clean reboot
  a16 pstore [ID] [--ssh]        pstore records shipped from the A16 (--ssh: its
                                 /var/lib/systemd/pstore, read-only)
  a16 mark 'text'                marker in the timeline (with $LAB_LEASE_HOLDER)
  a16 snap TOPIC --port c1|c2|a1|all --label TEXT [--since-last] [--mark TEXT]
                                 read-only diagnostic snapshot on the A16, pulled
                                 into ~/a16-snaps (docs/a16-snap.md); TOPIC usb
                                 display suspend audio net power redflags all;
                                 every snapshot counts kernel red flags
  a16 snap ls [TOPIC] [--label RE] | show ID | diff ID1 ID2   query that store

Changes the A16 (hold the a16 lease, LAB_LEASE_HOLDER=<you>):
  a16 serial send 'cmd' [--expect RE] [--wait S]   one line + CR, prints the reply
  a16 serial attach [--force]    interactive terminal, Ctrl-] quits
  a16 serial break               sends ^C
  a16 ssh [CMD...]               ssh to whichever A16 address answers first

Environment: A16_OBS_RUN (/run/a16-obs), A16_OBS_JOURNAL (/var/lib/a16-obs/journal),
A16_VM_URL (http://127.0.0.1:8428), A16_VL_URL (http://127.0.0.1:9428),
A16_LOG_BACKEND (auto|vl|journal), A16_HOSTNAME (zenbook-a16), A16_PON_REF,
A16_HOSTS (the A16's USB NIC and Wi-Fi addresses), A16_SSH_USER (root).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import socket
import subprocess
import sys
import termios
import time
import tty
import urllib.error
import urllib.parse
import urllib.request

from . import redflags as rf
from .telemetry import PON_MESSAGE_ID, PSTORE_MESSAGE_ID

RUN = os.environ.get("A16_OBS_RUN", "/run/a16-obs")
JOURNAL = os.environ.get("A16_OBS_JOURNAL", "/var/lib/a16-obs/journal")
VM = os.environ.get("A16_VM_URL", "http://127.0.0.1:8428").rstrip("/")
VL = os.environ.get("A16_VL_URL", "http://127.0.0.1:9428").rstrip("/")
BACKEND = os.environ.get("A16_LOG_BACKEND", "auto")  # auto | vl | journal
A16_HOST = os.environ.get("A16_HOSTNAME", "zenbook-a16")
HOSTS = os.environ.get("A16_HOSTS", "").split()
NICS = dict(zip(HOSTS, os.environ.get("A16_HOST_NICS", "usb wifi").split()))
SSH_USER = os.environ.get("A16_SSH_USER", "root")
MACHINE = os.environ.get("A16_OBS_MACHINE", "a16-obs")

SOURCES = {
    "serial": "a16-serial",
    "tx": "a16-serial-tx",
    "hb": "a16-heartbeat",
    "netconsole": "a16-netconsole",
    "mark": "a16-mark",
    "obs": "a16-obs",
}
TAG = {v: k for k, v in SOURCES.items()}


def holder() -> str:
    return os.environ.get("LAB_LEASE_HOLDER") or os.environ.get("USER") or "?"


def die(msg: str, code: int = 1):
    print(f"a16: {msg}", file=sys.stderr)
    sys.exit(code)


# --- sockets ---------------------------------------------------------------------
def connect(name: str, hello: dict, timeout: float = 5.0) -> tuple[socket.socket, dict, bytes]:
    path = os.path.join(RUN, name)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
    except OSError as e:
        die(f"cannot connect to {path}: {e.strerror} (is the a16-obs container running?)", 3)
    s.sendall((json.dumps(hello) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    try:
        reply = json.loads(line or b"{}")
    except ValueError:
        reply = {"ok": False, "error": f"bad reply {line[:100]!r}"}
    return s, reply, rest


def serial_status() -> dict | None:
    path = os.path.join(RUN, "serial.sock")
    if not os.path.exists(path):
        return None
    try:
        s, reply, _ = connect("serial.sock", {"mode": "status"}, 3)
        s.close()
        return reply
    except SystemExit:
        return None


# --- logs: VictoriaLogs (LogsQL) or the container journal (journalctl) ------------
# A source is a list of alternatives, each a conjunction of (field, value)
# matches: the same shape works as journalctl matches ("+" between
# alternatives) and as a LogsQL filter.
def source_terms(name: str) -> list[list[tuple[str, str]]]:
    if name in SOURCES:
        return [[("SYSLOG_IDENTIFIER", SOURCES[name])]]
    if name == "journal":
        return [[("_HOSTNAME", A16_HOST)]]
    if name == "kernel":
        return [[("_HOSTNAME", A16_HOST), ("_TRANSPORT", "kernel")]]
    if name == "boot":
        return [[("MESSAGE_ID", PON_MESSAGE_ID)]]
    if name == "pstore":
        return [[("MESSAGE_ID", PSTORE_MESSAGE_ID)]]
    if name.startswith("a16:"):
        return [[("_HOSTNAME", A16_HOST), ("SYSLOG_IDENTIFIER", name[4:])]]
    return [[("SYSLOG_IDENTIFIER", name)]]


def terms_for(sources: list[str]) -> list[list[tuple[str, str]]]:
    out = []
    for s in sources:
        for alt in source_terms(s):
            if alt not in out:
                out.append(alt)
    return out


def parse_time(s: str | None, now: float | None = None) -> float | None:
    """Epoch seconds for journalctl-style times: 10m/2h/1d (ago), -10min,
    @EPOCH, now/today/yesterday, ISO dates, HH:MM[:SS] (today), else date -d."""
    if s is None:
        return None
    now = time.time() if now is None else now
    m = re.match(r"^-?(\d+(?:\.\d+)?)(s|sec|m|min|h|d|w)$", s)
    if m:
        mult = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return now - float(m.group(1)) * mult
    if s.startswith("@"):
        return float(s[1:])
    if s == "now":
        return now
    if s in ("today", "yesterday"):
        t = time.localtime(now)
        midnight = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        return midnight - (86400 if s == "yesterday" else 0)
    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", s):
        t = time.localtime(now)
        hms = [int(x) for x in s.split(":")] + [0]
        return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hms[0], hms[1], hms[2], 0, 0, -1))
    try:
        import datetime

        d = datetime.datetime.fromisoformat(s)
        return d.timestamp()
    except ValueError:
        pass
    try:
        out = subprocess.run(["date", "-d", s, "+%s.%N"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    raise ValueError(f"can't parse time {s!r}")


def journal_args() -> list[str]:
    if os.access(JOURNAL, os.R_OK | os.X_OK):
        return ["journalctl", "-D", JOURNAL]
    if os.geteuid() == 0:
        return ["journalctl", "-M", MACHINE]
    die(f"cannot read {JOURNAL} (are you in the a16-lab group? log in again after it was added)")


def journal_cmd(terms, since=None, until=None, lines=None, follow=False, grep=None) -> list[str]:
    # -a: journalctl's JSON output nulls fields over 4 KiB otherwise (pstore text).
    cmd = journal_args() + ["-o", "json", "-a", "--no-pager"]
    if since is not None:
        cmd += ["--since", f"@{since:.6f}"]
    if until is not None:
        cmd += ["--until", f"@{until:.6f}"]
    if lines:
        cmd += ["-n", str(lines)]
    if follow:
        cmd += ["-f"]
    if grep:
        cmd += ["-g", grep]
    for i, alt in enumerate(terms):
        if i:
            cmd.append("+")
        cmd += [f"{k}={v}" for k, v in alt]
    return cmd


def smart_case(rx: str) -> str:
    """journalctl -g semantics: case-insensitive unless the pattern has capitals."""
    return rx if re.search(r"[A-Z]", re.sub(r"\\.", "", rx)) else "(?i)" + rx


def logsql_filter(terms, grep=None) -> str:
    alts = []
    for alt in terms:
        alts.append("(" + " ".join(f"{k}:={json.dumps(v)}" for k, v in alt) + ")")
    f = "(" + " or ".join(alts) + ")" if alts else "*"
    if grep:
        f += f" _msg:~{json.dumps(smart_case(grep))}"
    return f


def logsql_query(terms, lines=None, grep=None) -> str:
    q = logsql_filter(terms, grep)
    if lines:
        return q + f" | sort by (_time desc) | limit {int(lines)}"
    return q + " | sort by (_time)"


def rfc3339_us(t: str) -> int:
    """VictoriaLogs' _time (RFC 3339, up to ns, Z) as epoch microseconds."""
    import datetime

    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$", t)
    if not m:
        return 0
    base = datetime.datetime.fromisoformat(m.group(1) + (m.group(3) if m.group(3) != "Z" else "+00:00"))
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return int(base.timestamp()) * 1_000_000 + int(frac)


def vl_entry(o: dict) -> dict:
    e = dict(o)
    e["MESSAGE"] = o.get("_msg", "")
    e["__REALTIME_TIMESTAMP"] = str(rfc3339_us(o.get("_time", "")))
    return e


class VLError(Exception):
    pass


def vl_available(timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{VL}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def vl_entries(terms, since=None, until=None, lines=None, grep=None, timeout=30.0):
    params = {"query": logsql_query(terms, lines, grep)}
    if since is not None:
        params["start"] = f"{since:.6f}"
    if until is not None:
        params["end"] = f"{until:.6f}"
    req = urllib.request.Request(f"{VL}/select/logsql/query", data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = [vl_entry(json.loads(line)) for line in r if line.strip()]
    except urllib.error.HTTPError as e:
        raise VLError(f"VictoriaLogs: {e.code} {e.read()[:300].decode('utf-8', 'replace')}") from e
    except (OSError, ValueError) as e:
        raise VLError(f"VictoriaLogs: {e}") from e
    if lines:
        out.reverse()
    return out


def journal_entries(terms, since=None, until=None, lines=None, grep=None):
    out = []
    try:
        p = subprocess.run(journal_cmd(terms, since, until, lines, False, grep), capture_output=True, text=True)
    except FileNotFoundError:
        die("journalctl not found")
    for line in p.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def backend(want: str | None = None) -> str:
    want = want or BACKEND
    if want == "auto":
        return "vl" if vl_available() else "journal"
    return want


def fetch(terms, since=None, until=None, lines=None, grep=None, want=None) -> list[dict]:
    """Entries from VictoriaLogs if it answers, else from the journal files."""
    b = backend(want)
    if b == "vl":
        try:
            return vl_entries(terms, since, until, lines, grep)
        except VLError as e:
            if (want or BACKEND) == "vl":
                die(str(e))
            print(f"a16: {e}; falling back to journalctl", file=sys.stderr)
    return journal_entries(terms, since, until, lines, grep)


def field(e: dict, k: str, default=None):
    v = e.get(k, default)
    if isinstance(v, list):  # journalctl: binary values as byte arrays
        try:
            v = bytes(v).decode("utf-8", "backslashreplace")
        except (TypeError, ValueError):
            v = str(v)
    return v


def fmt_entry(e: dict, show_source=True) -> str:
    try:
        t = int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6
    except ValueError:
        t = 0
    ts = time.strftime("%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t * 1000) % 1000:03d}"
    msg = field(e, "MESSAGE", "")
    ident = field(e, "SYSLOG_IDENTIFIER", "?")
    if e.get("_HOSTNAME") == A16_HOST and ident not in TAG:
        src = f"a16/{ident}"
    else:
        src = TAG.get(ident, ident)
    extra = ""
    if ident == "a16-serial-tx" or ident == "a16-mark":
        extra = f"[{e.get('A16_HOLDER') or '?'}] "
    if e.get("A16_SERIAL_PARTIAL") == "1":
        extra += "(partial) "
    if show_source:
        return f"{ts} {src:<10} {extra}{msg}"
    return f"{ts} {extra}{msg}"


def run_journal(sources: list[str], since=None, until=None, lines=None, follow=False, grep=None, raw=False, show_source=True, want=None):
    terms = terms_for(sources)
    try:
        t0, t1 = parse_time(since), parse_time(until)
    except ValueError as e:
        die(str(e))

    def show(e):
        print(field(e, "MESSAGE", "") if raw else fmt_entry(e, show_source), flush=follow)

    if not follow:
        try:
            for e in fetch(terms, t0, t1, lines, grep, want):
                show(e)
        except BrokenPipeError:
            pass
        return 0
    # Following reads the journal files directly: no ingestion lag.
    try:
        p = subprocess.Popen(journal_cmd(terms, t0, t1, lines, True, grep), stdout=subprocess.PIPE, text=True)
    except FileNotFoundError:
        die("journalctl not found")
    try:
        for line in p.stdout:
            try:
                show(json.loads(line))
            except ValueError:
                continue
    except (KeyboardInterrupt, BrokenPipeError):
        p.terminate()
    return p.wait()


# --- VictoriaMetrics -------------------------------------------------------------
def vm_get(path: str, params: dict, timeout=5.0) -> dict:
    url = f"{VM}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def vm_query(q: str, timeout=5.0) -> list:
    try:
        return vm_get("/api/v1/query", {"query": q}, timeout)["data"]["result"]
    except Exception:  # noqa: BLE001 - status must work with VictoriaMetrics down
        return []


SHORTCUTS = {
    "up": 'up{job="a16"}',
    "temps": 'max by (type) (node_thermal_zone_temp{job="a16",nic="usb"} or node_thermal_zone_temp{job="a16"})',
    "power": "a16_battery_power_watts or a16_hb_bat_uw / 1e6",
    "battery": "a16_battery_capacity_percent",
    "residency": "rate(a16_qcom_stats_seconds[5m])",
    "wakeups": "topk(10, rate(a16_wakeup_event_count[5m]))",
    "hb": "a16_hb_uptime_seconds",
    "boot": "a16_boot_info",
    "suspend": "a16_suspend_stats",
    "redflags": 'a16_redflag_total{severity!="info"} > 0',
}


def fmt_metric(r: dict) -> str:
    m = dict(r.get("metric", {}))
    name = m.pop("__name__", "")
    for k in ("job", "host"):
        m.pop(k, None)
    labels = ",".join(f'{k}="{v}"' for k, v in sorted(m.items()))
    if "value" in r:
        return f"{name}{{{labels}}} {r['value'][1]}"
    vals = " ".join(v for _, v in r.get("values", [])[-20:])
    return f"{name}{{{labels}}} [{vals}]"


# --- red flags -------------------------------------------------------------------
# Known-bad kernel log lines (redflags.py) over the uploaded A16 kernel log,
# netconsole and the serial console. The same kernel line usually arrives by
# two or three of those, so lines are deduplicated on (kernel timestamp,
# message): the journal's _SOURCE_MONOTONIC_TIMESTAMP, netconsole's
# A16_KTIME_USEC and the serial line's "[    s.us]" prefix are the same
# printk clock.
REDFLAG_SOURCES = ("kernel", "netconsole", "serial")


def vl_stats(query: str, since=None, until=None, timeout=10.0) -> list[dict]:
    params = {"query": query}
    if since is not None:
        params["start"] = f"{since:.6f}"
    if until is not None:
        params["end"] = f"{until:.6f}"
    req = urllib.request.Request(f"{VL}/select/logsql/query", data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return [json.loads(line) for line in r if line.strip()]
    except urllib.error.HTTPError as e:
        raise VLError(f"VictoriaLogs: {e.code} {e.read()[:300].decode('utf-8', 'replace')}") from e
    except (OSError, ValueError) as e:
        raise VLError(f"VictoriaLogs: {e}") from e


def boot_window(boot: str | None) -> tuple[float | None, float | None, str | None]:
    """(start, end or None, boot id) of a boot: "current" from the heartbeat
    (its time minus its uptime, which counts suspend), an id prefix from the
    first and last A16 kernel log lines of that boot in VictoriaLogs (the end
    padded by 2 minutes for serial/netconsole lines the journal lost)."""
    if boot in (None, "current"):
        hb = read_heartbeat()
        if hb and hb.get("uptime") is not None:
            return float(hb["t"]) - float(hb["uptime"]) - 5, None, hb.get("boot_id")
        boot = None
    q = f"_HOSTNAME:={json.dumps(A16_HOST)} _TRANSPORT:=kernel"
    if boot:
        q += f" _BOOT_ID:{norm_id(boot)}*"
    q += " | stats by (_BOOT_ID) min(_time) first, max(_time) last | sort by (last desc) | limit 1"
    try:
        rows = vl_stats(q, since=time.time() - 30 * 86400)
    except VLError as e:
        die(f"can't find boot {boot or 'current'}: {e}")
    if not rows:
        die(f"no A16 kernel log for boot {boot or 'current'} in the last 30 days")
    r = rows[0]
    end = rfc3339_us(r["last"]) / 1e6 + 120 if boot else None
    return rfc3339_us(r["first"]) / 1e6 - 5, end, r["_BOOT_ID"]


def redflag_event(e: dict) -> tuple[tuple, str, str, float]:
    """(dedup key, source, message, epoch) of a log entry."""
    ident = field(e, "SYSLOG_IDENTIFIER", "")
    msg = field(e, "MESSAGE", "") or ""
    try:
        t = int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6
    except ValueError:
        t = 0.0
    usec = None
    if ident == SOURCES["serial"]:
        src = "serial"
        usec, msg = rf.split_ts(msg)
    elif ident == SOURCES["netconsole"]:
        src = "netconsole"
        raw_usec, msg = rf.split_ts(msg)  # some entries are raw records
        usec = e.get("A16_KTIME_USEC") or raw_usec
    else:
        src = "kernel"
        usec = e.get("_SOURCE_MONOTONIC_TIMESTAMP")
    try:
        usec = int(usec) if usec is not None else None
    except (TypeError, ValueError):
        usec = None
    key = (usec, msg) if usec is not None else (src, t, msg)
    return key, src, msg, t


def redflags_scan(entries, min_sev: str = "info") -> tuple[rf.Tally, list[tuple[float, rf.Pattern, str, set]]]:
    """Tally of unique red flag lines (deduplicated across sources) and the
    lines in time order: (epoch, pattern, message, sources)."""
    seen: dict[tuple, list] = {}
    for e in entries:
        key, src, msg, t = redflag_event(e)
        ev = seen.get(key)
        if ev is not None:
            ev[3].add(src)
            ev[0] = min(ev[0], t)
            continue
        p = rf.classify(msg)
        if p is None or rf.SEV_RANK[p.sev] < rf.SEV_RANK[min_sev]:
            continue
        seen[key] = [t, p, msg, {src}]
    events = sorted((tuple(v) for v in seen.values()), key=lambda v: v[0])
    tally = rf.Tally()
    for t, p, msg, srcs in events:
        tally.add(msg, t=t, pattern=p)
        tally.hits[p.id].sources |= srcs
    return tally, events


def fmt_t(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t)) if t else "-"


def cmd_redflags(a) -> int:
    sources = a.source.split(",") if a.source else list(REDFLAG_SOURCES)
    try:
        until = parse_time(a.until)
        since = parse_time(a.since)
    except ValueError as e:
        die(str(e))
    boot_id = None
    if since is None:
        since, bend, boot_id = boot_window(a.boot or "current")
        until = until or bend
    terms = terms_for(sources)
    entries = fetch(terms, since, until, grep=rf.combined_regex(a.sev), want=a.backend)
    tally, events = redflags_scan(entries, a.sev)
    hits = tally.ranked(a.sev)
    if a.json:
        print(json.dumps({
            "since": since, "until": until, "boot_id": boot_id, "sources": sources, "min_sev": a.sev,
            "by_severity": tally.by_severity(),
            "patterns": [{
                "id": h.pattern.id, "sev": h.pattern.sev, "subsystem": h.pattern.subsystem, "bead": h.pattern.bead_id,
                "count": h.count, "first_t": h.first_t, "last_t": h.last_t, "first": h.first, "last": h.last,
                "sources": sorted(h.sources),
            } for h in hits],
        }, indent=1))
        return 0
    scope = f"boot {str(boot_id)[:8]} " if boot_id else ""
    print(f"red flags (>= {a.sev}) {scope}{fmt_t(since)} .. {fmt_t(until) if until else 'now'}, sources {','.join(sources)}")
    if not hits:
        print("  none")
        return 0
    print(f"  {'SEV':<4} {'COUNT':>5}  {'ID':<24} {'SUBSYS':<10} {'BEAD':<5} {'SRC':<3} {'LAST':<14}  LAST LINE")
    for h in hits:
        p = h.pattern
        src = "".join(s[0] for s in REDFLAG_SOURCES if s in h.sources)
        print(f"  {p.sev:<4} {h.count:>5}  {p.id:<24} {p.subsystem:<10} {p.bead or '-':<5} {src:<3} {fmt_t(h.last_t):<14}  {h.last[:110]}")
    by = tally.by_severity()
    print("  " + ", ".join(f"{k} {v}" for k, v in by.items() if rf.SEV_RANK[k] >= rf.SEV_RANK[a.sev])
          + "  (SRC: k kernel log, n netconsole, s serial; a line seen by several counts once)")
    if a.lines:
        print()
        for t, p, msg, srcs in events:
            print(f"{fmt_t(t)} {p.sev:<4} {p.id:<24} [{','.join(sorted(srcs))}] {msg}")
    return 0


def status_redflags(timeout: float = 5.0) -> dict | None:
    """Red flags in the current boot's uploaded kernel log, for `a16 status`:
    {"by_severity", "top": [(id, n)], "boot_id"}; None if unknown."""
    hb = read_heartbeat()
    if not hb or hb.get("uptime") is None or not vl_available():
        return None
    since = float(hb["t"]) - float(hb["uptime"]) - 5
    bid = norm_id(hb.get("boot_id"))
    q = (
        f"_HOSTNAME:={json.dumps(A16_HOST)} _TRANSPORT:=kernel _BOOT_ID:={json.dumps(bid)} "
        f"_msg:~{json.dumps(rf.combined_regex('info'))} | stats by (_msg) count() n"
    )
    try:
        rows = vl_stats(q, since=since, timeout=timeout)
    except VLError:
        return None
    tally = rf.Tally()
    for r in rows:
        p = rf.classify(r.get("_msg", ""))
        if p is not None:
            tally.add(r["_msg"], pattern=p, n=int(r.get("n", 1)))
    return {
        "boot_id": hb.get("boot_id"),
        "by_severity": tally.by_severity(),
        "top": [(h.pattern.id, h.count) for h in tally.ranked("warn")[:3]],
    }


# --- status ----------------------------------------------------------------------
def read_heartbeat() -> dict | None:
    try:
        with open(os.path.join(RUN, "heartbeat.last")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def tcp_probe(host: str, port: int = 22, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def lease_status() -> str | None:
    try:
        out = subprocess.run(["lab-lease", "status", "a16"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [ln for ln in out.splitlines() if ln.startswith("a16")]
    return " ".join(lines[0].split()) if lines else None


def gather_status(probe=True) -> dict:
    now = time.time()
    st: dict = {"time": now}
    hb = read_heartbeat()
    if hb:
        hb["age"] = round(now - hb.get("t", 0), 1)
    st["heartbeat"] = hb
    st["serial"] = serial_status()
    up = {}
    for r in vm_query('last_over_time(up{job="a16"}[1m])'):
        up[r["metric"].get("nic", r["metric"].get("instance", "?"))] = int(float(r["value"][1]))
    st["scrape_up"] = up
    info = vm_query('last_over_time(a16_boot_info[5m])')
    st["boot_info"] = info[0]["metric"] if info else None
    failed = vm_query('max(count(node_systemd_unit_state{job="a16",state="failed"} == 1) by (nic))')
    st["failed_units"] = int(float(failed[0]["value"][1])) if failed else None
    st["ssh"] = {}
    if probe:
        for h in HOSTS:
            st["ssh"][NICS.get(h, h)] = tcp_probe(h)
    st["paused"] = metrics_paused()
    st["lease"] = lease_status()
    st["redflags"] = status_redflags()
    if hb and hb["age"] < 5:
        st["verdict"], st["code"] = "up", 0
    elif hb and hb.get("state") == "suspending":
        st["verdict"], st["code"] = "suspended", 1
    elif any(st["ssh"].values()):
        st["verdict"], st["code"] = "up (no heartbeat)", 0
    else:
        st["verdict"], st["code"] = "silent", 2
    return st


def print_status(st: dict) -> None:
    hb = st["heartbeat"]
    print(f"A16: {st['verdict']}")
    if hb:
        bat = ""
        if "bat_pct" in hb:
            w = int(hb.get("bat_uw", 0)) / 1e6
            bat = f", battery {hb['bat_pct']}% {w:+.1f} W, ac {hb.get('ac', '?')}"
        temp = f", max {int(hb['temp_max_mc']) / 1000:.1f} C" if "temp_max_mc" in hb else ""
        print(
            f"  heartbeat {hb['age']}s ago via {hb.get('src')}: boot {hb.get('boot_id')}, "
            f"uptime {float(hb.get('uptime', 0)):.0f}s, state {hb.get('state', '?')}{bat}{temp}"
        )
    else:
        print("  heartbeat: none received yet")
    bi = st["boot_info"]
    if bi:
        print(f"  generation {bi.get('generation') or '?'}, kernel {bi.get('kernel')}, entry {bi.get('entry')}")
    paths = []
    for nic in sorted(set(list(st["ssh"]) + list(st["scrape_up"]))):
        bits = []
        if nic in st["ssh"]:
            bits.append("ssh " + ("ok" if st["ssh"][nic] else "no"))
        if nic in st["scrape_up"]:
            bits.append("scrape " + ("up" if st["scrape_up"][nic] else "down"))
        paths.append(f"{nic}: {', '.join(bits)}")
    if paths:
        print("  " + "; ".join(paths) + (" (scrape paused)" if st["paused"] else ""))
    if st["failed_units"] is not None:
        print(f"  failed units: {st['failed_units']}")
    s = st["serial"]
    if s:
        dev = "open" if s.get("open") else f"closed ({s.get('error')})"
        last = ""
        if s.get("last_line_age") is not None:
            last = f", last line {s['last_line_age']:.0f}s ago: {s.get('last_line', '')[:80]!r}"
        w = f", writer {s['writer']}" if s.get("writer") else ""
        print(f"  serial: {dev}{w}{last}")
    else:
        print("  serial: daemon not reachable")
    if st["lease"]:
        print(f"  lease: {st['lease']}")
    rfs = st.get("redflags")
    if rfs:
        print(f"  red flags this boot: {rf.summary_line(rfs['by_severity'], rfs['top'])}  (a16 redflags)")


def cmd_status(a) -> int:
    st = gather_status(probe=not a.no_probe)
    if a.json:
        print(json.dumps(st, indent=1, default=str))
    else:
        print_status(st)
    return st["code"]


# --- serial ----------------------------------------------------------------------
def cmd_serial(a) -> int:
    if a.action == "tail":
        return run_journal(["serial"], since=a.since, lines=a.lines, follow=a.follow, grep=a.grep, show_source=False)
    if a.action == "status":
        s = serial_status()
        if s is None:
            die("serial daemon not reachable", 3)
        print(json.dumps(s, indent=1))
        return 0 if s.get("open") else 1
    if a.action == "follow":
        s, reply, rest = connect("serial.sock", {"mode": "read", "backlog": a.backlog})
        if not reply.get("ok"):
            die(reply.get("error", "refused"))
        s.settimeout(None)
        out = sys.stdout.buffer
        try:
            if rest:
                out.write(rest)
                out.flush()
            while True:
                d = s.recv(4096)
                if not d:
                    return 0
                out.write(d)
                out.flush()
        except (KeyboardInterrupt, BrokenPipeError):
            return 0
    if a.action in ("send", "break"):
        data = b"\x03" if a.action == "break" else (a.text.encode() + (b"" if a.no_cr else b"\r"))
        return serial_send(data, a.wait, a.quiet, a.expect, a.force)
    if a.action == "attach":
        return serial_attach(a.force)
    return 2


def serial_send(data: bytes, wait: float, quiet: float, expect: str | None, force: bool) -> int:
    s, reply, rest = connect("serial.sock", {"mode": "write", "holder": holder(), "force": force})
    if not reply.get("ok"):
        die(reply.get("error", "refused"), 4)
    if not reply.get("open"):
        print("a16: warning: serial device is not open; the input will be dropped", file=sys.stderr)
    s.sendall(data)
    got = bytearray(rest)
    rx = re.compile(expect.encode()) if expect else None
    deadline = time.monotonic() + wait
    last = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= deadline or (rx is None and got and now - last >= quiet):
            break
        if rx is not None and rx.search(got):
            break
        r, _, _ = select.select([s], [], [], min(0.1, max(0.0, deadline - now)))
        if r:
            d = s.recv(4096)
            if not d:
                break
            got += d
            last = time.monotonic()
    s.close()
    text = bytes(got).replace(b"\r", b"")
    sys.stdout.write(text.decode("utf-8", "backslashreplace"))
    if text and not text.endswith(b"\n"):
        sys.stdout.write("\n")
    if rx is not None and not rx.search(got):
        print(f"a16: no match for {expect!r} within {wait}s", file=sys.stderr)
        return 1
    return 0


def serial_attach(force: bool) -> int:
    s, reply, rest = connect("serial.sock", {"mode": "write", "holder": holder(), "force": force, "backlog": 2048})
    if not reply.get("ok"):
        die(reply.get("error", "refused"), 4)
    s.settimeout(None)
    print(f"a16: attached as {holder()}; Ctrl-] quits\r", file=sys.stderr)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    out = sys.stdout.buffer
    if rest:
        out.write(rest)
        out.flush()
    try:
        if old:
            tty.setraw(fd)
        while True:
            r, _, _ = select.select([s, fd], [], [])
            if s in r:
                d = s.recv(4096)
                if not d:
                    print("\r\na16: connection closed by the serial daemon\r", file=sys.stderr)
                    return 1
                out.write(d)
                out.flush()
            if fd in r:
                d = os.read(fd, 1024)
                if not d or b"\x1d" in d:
                    d = d.split(b"\x1d")[0]
                    if d:
                        s.sendall(d)
                    return 0
                s.sendall(d)
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print("\r\na16: detached", file=sys.stderr)


# --- metrics ---------------------------------------------------------------------
def vm_dir() -> str:
    return os.path.join(RUN, "vmagent")


def metrics_paused() -> bool:
    return os.path.exists(os.path.join(vm_dir(), "paused"))


def cmd_metrics(a) -> int:
    d = vm_dir()
    active = os.path.join(d, "active", "a16.json")
    if a.name == "pause":
        open(os.path.join(d, "paused"), "w").close()
        try:
            os.unlink(active)
        except FileNotFoundError:
            pass
        mark_text(f"metrics scrape paused ({a.reason or 'no reason given'})", quiet=True)
        print("a16: vmagent scrape of the A16 paused (takes effect within ~5 s); `a16 metrics resume` to undo")
        return 0
    if a.name == "resume":
        default = os.path.join(d, "a16.json.default")
        if not os.path.exists(default):
            die(f"{default} missing (container not started?)")
        with open(default) as f:
            content = f.read()
        tmp = active + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.replace(tmp, active)
        try:
            os.unlink(os.path.join(d, "paused"))
        except FileNotFoundError:
            pass
        mark_text("metrics scrape resumed", quiet=True)
        print("a16: vmagent scrape of the A16 resumed")
        return 0
    if a.name == "state":
        print("paused" if metrics_paused() else "scraping")
        return 0
    q = a.query or SHORTCUTS.get(a.name or "up", a.name)
    try:
        if a.range:
            end = time.time()
            secs = parse_duration(a.range)
            res = vm_get(
                "/api/v1/query_range",
                {"query": q, "start": end - secs, "end": end, "step": a.step or max(15, int(secs / 60))},
            )["data"]["result"]
        else:
            res = vm_get("/api/v1/query", {"query": q})["data"]["result"]
    except Exception as e:  # noqa: BLE001
        die(f"query failed: {e}")
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    if not res:
        print(f"(no data for {q})")
        return 1
    for r in res:
        print(fmt_metric(r))
    return 0


def parse_duration(s: str) -> float:
    m = re.match(r"^(\d+(?:\.\d+)?)([smhd]?)$", s)
    if not m:
        die(f"bad duration {s!r}")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


# --- boots -----------------------------------------------------------------------
def hb_boots(events: list[dict]) -> list[dict]:
    """Boots as the heartbeat sink saw them, from its journal events."""
    boots: list[dict] = []
    cur = None
    for e in events:
        ev = e.get("A16_EVENT")
        t = int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        bid = e.get("A16_BOOT_ID")
        if ev == "boot":
            if cur is not None and cur["boot_id"] == e.get("A16_PREV_BOOT_ID"):
                cur["last_uptime"] = e.get("A16_PREV_UPTIME")
                cur["last_state"] = e.get("A16_PREV_STATE")
            try:
                up = float(e.get("A16_UPTIME") or 0)
            except ValueError:
                up = 0.0
            cur = {"boot_id": bid, "start": t - up, "lost": None, "end": "up", "hb": True}
            boots.append(cur)
        elif cur is not None and bid == cur["boot_id"]:
            if ev == "lost":
                cur["lost"] = t
                cur["last_uptime"] = e.get("A16_UPTIME")
                cur["last_state"] = e.get("A16_STATE")
            elif ev == "resumed":
                cur["lost"] = None
    for i, b in enumerate(boots):
        if i + 1 < len(boots):
            st = b.get("last_state") or "?"
            b["end"] = {"suspending": "lost while suspending"}.get(st, f"ended (last state {st})")
        elif b["lost"]:
            b["end"] = f"silent since {time.strftime('%H:%M:%S', time.localtime(b['lost']))} (last state {b.get('last_state')})"
    return boots


def pon_of(rec: dict) -> dict[str, str]:
    return {k[len("A16_PON_") :].lower(): str(field(rec, k)) for k in rec if re.match(r"^A16_PON_[A-Z0-9]+$", k)}


def pon_diff(cur: dict[str, str], ref: dict[str, str], limit: int = 8) -> str:
    """Bytes that differ from the reference, as name[+off]=new(ref old)."""
    parts = []
    for name in sorted(set(cur) | set(ref)):
        if name not in cur or name not in ref:
            parts.append(f"{name} missing")
            continue
        try:
            a, b = bytes.fromhex(cur[name]), bytes.fromhex(ref[name])
        except ValueError:
            parts.append(f"{name} unreadable")
            continue
        diffs = [i for i in range(max(len(a), len(b))) if (a[i : i + 1] or b"?") != (b[i : i + 1] or b"?")]
        if not diffs:
            parts.append(f"{name} =ref")
            continue
        shown = " ".join(f"+{i:02x}={a[i]:02x}({b[i]:02x})" if i < len(a) and i < len(b) else f"+{i:02x}=?" for i in diffs[:limit])
        more = f" +{len(diffs) - limit} more" if len(diffs) > limit else ""
        parts.append(f"{name} {shown}{more}")
    return "; ".join(parts)


def pick_reference(records: list[dict], ref: str | None) -> dict | None:
    """The boot record holding the reference PON dump: --ref (a boot id
    prefix), else the newest record whose previous boot ended in a clean
    reboot (then any orderly shutdown)."""
    if ref:
        for r in reversed(records):
            if str(r.get("A16_BOOT_ID", "")).replace("-", "").startswith(ref.replace("-", "")):
                return r
        die(f"no boot record for --ref {ref}")
    for want in ("reboot", "shutdown"):
        for r in reversed(records):
            if r.get("A16_PREVIOUS_END") == want and pon_of(r):
                return r
    return None


def norm_id(b: str | None) -> str:
    return (b or "").replace("-", "").lower()


def build_boots(events: list[dict], records: list[dict], ref: dict | None) -> list[dict]:
    boots = {norm_id(b["boot_id"]): b for b in hb_boots(events)}
    order = list(boots)
    for r in records:
        bid = norm_id(r.get("A16_BOOT_ID"))
        if not bid:
            continue
        t = int(r.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        b = boots.get(bid)
        if b is None:
            b = boots[bid] = {"boot_id": r.get("A16_BOOT_ID"), "start": t, "end": "?", "hb": False}
            order.append(bid)
        b["record"] = r
    seq = sorted(order, key=lambda k: boots[k]["start"])
    # A record's PON dump and its previous_end describe how the boot before it
    # ended: attach them there (by A16_PREVIOUS_BOOT_ID, else the boot listed
    # just before).
    for i, k in enumerate(seq):
        r = boots[k].get("record")
        if not r:
            continue
        prev = norm_id(r.get("A16_PREVIOUS_BOOT_ID"))
        if not prev and i > 0:
            prev = seq[i - 1]
        if prev and prev in boots:
            target = boots[prev]
        elif prev:
            target = boots[prev] = {"boot_id": prev, "start": boots[k]["start"] - 1, "end": "?", "hb": False}
            seq.insert(i, prev)
        else:
            continue
        target["ended"] = r.get("A16_PREVIOUS_END")
        pon = pon_of(r)
        if pon:
            target["end_pon"] = pon
            if ref is not None:
                target["end_pon_diff"] = "reference" if r is ref else pon_diff(pon, pon_of(ref))
    return [boots[k] for k in sorted(seq, key=lambda k: boots[k]["start"])]


def cmd_boots(a) -> int:
    try:
        since = parse_time(a.since)
    except ValueError as e:
        die(str(e))
    events = fetch(terms_for(["hb"]), since, want=a.backend)
    records = fetch(terms_for(["boot"]), since, want=a.backend)
    ref = pick_reference(records, a.ref or os.environ.get("A16_PON_REF"))
    boots = build_boots(events, records, ref)[-a.n :]
    if a.json:
        print(json.dumps({"reference": ref, "boots": boots}, indent=1, default=str))
        return 0
    if not boots:
        print("(no boots seen: no heartbeat events or boot records yet)")
        return 0
    info = {r["metric"].get("boot_id"): r["metric"] for r in vm_query("last_over_time(a16_boot_info[90d])", 10)}
    if ref is not None:
        print(
            f"PON reference: dump read at boot {str(ref.get('A16_BOOT_ID'))[:8]} "
            f"(the boot before it ended: {ref.get('A16_PREVIOUS_END')}); PON columns show the dump read at the "
            "next boot, i.e. how each boot ended, as +offset=value(reference)"
        )
    else:
        print("PON reference: none yet (no boot record after a clean reboot; pick one with --ref BOOT_ID)")
    for b in boots:
        r = b.get("record") or {}
        mi = info.get(b["boot_id"], {})
        gen = r.get("A16_GENERATION") or mi.get("generation") or "?"
        kernel = r.get("A16_KERNEL") or mi.get("kernel") or "?"
        start = time.strftime("%m-%d %H:%M:%S", time.localtime(b["start"]))
        up = b.get("last_uptime")
        upt = f"{float(up) / 3600:.2f}h" if up else "-"
        end = b.get("end", "?")
        if b.get("ended"):
            end += f"; next boot found its journal ending: {b['ended']}"
        pon = b.get("end_pon_diff") or ("(no reference)" if "end_pon" in b else "")
        print(f"{start}  {str(b['boot_id'])[:8]}  gen {gen:>3}  {kernel:<16} up {upt:>7}  {end}")
        if pon:
            print(f"{'':>17}PON at end: {pon}")
    return 0


# --- ssh, pstore, mark -----------------------------------------------------------
def pick_host() -> str:
    for h in HOSTS:
        if tcp_probe(h, 22, 2.0):
            return h
    die("no A16 address answers on port 22: " + " ".join(HOSTS), 2)


def cmd_ssh(a) -> int:
    h = pick_host()
    argv = ["ssh", "-o", "ConnectTimeout=5", "-o", f"HostKeyAlias={HOSTS[0]}", f"{SSH_USER}@{h}"] + a.cmd
    os.execvp("ssh", argv)


def cmd_pstore(a) -> int:
    """Records shipped by a16-pstore-ship (the A16 journal on the observer host); --ssh
    reads /var/lib/systemd/pstore on the A16 instead."""
    if not a.ssh:
        recs = fetch(terms_for(["pstore"]), want=a.backend)
        if a.id:
            for r in reversed(recs):
                if field(r, "A16_PSTORE_ID") == a.id:
                    print(f"== pstore record {a.id} (found at boot {field(r, '_BOOT_ID', '?')}, files {field(r, 'A16_PSTORE_FILES', '')})")
                    sys.stdout.write(field(r, "A16_PSTORE_TEXT", "") or "")
                    return 0
            print(f"a16: {a.id} not in the shipped records; asking the A16", file=sys.stderr)
        else:
            if not recs:
                print("(no pstore records shipped; `a16 pstore --ssh` lists the A16's directory)")
            for r in recs:
                t = int(r.get("__REALTIME_TIMESTAMP", "0")) / 1e6
                print(
                    f"{field(r, 'A16_PSTORE_ID', '?'):<12} crash {field(r, 'A16_PSTORE_TIME') or '?':<19}  "
                    f"shipped {time.strftime('%m-%d %H:%M', time.localtime(t))}  {field(r, 'MESSAGE', '')}"
                )
            return 0
    h = pick_host()
    if a.id:
        if not re.match(r"^[A-Za-z0-9._-]+$", a.id):
            die("bad record id")
        remote = f"cd /var/lib/systemd/pstore/{a.id} && find . -type f | sort | while read -r f; do echo \"== $f\"; cat \"$f\"; done"
    else:
        remote = "ls -l --time-style=+%F_%T /var/lib/systemd/pstore/ 2>/dev/null; ls /sys/fs/pstore 2>/dev/null"
    return subprocess.call(["ssh", "-o", "ConnectTimeout=5", f"{SSH_USER}@{h}", remote])


def mark_text(text: str, quiet=False) -> int:
    path = os.path.join(RUN, "mark.sock")
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(path)
        s.sendall((json.dumps({"text": text, "holder": holder()}) + "\n").encode())
        reply = json.loads(s.recv(4096) or b"{}")
        s.close()
    except (OSError, ValueError) as e:
        if quiet:
            return 1
        die(f"mark failed: {e}")
    if not reply.get("ok"):
        if quiet:
            return 1
        die(f"mark refused: {reply.get('error')}")
    return 0


def cmd_mark(a) -> int:
    return mark_text(" ".join(a.text))


def cmd_snap(a) -> int:
    from .snap import main as snapmain
    from .snap import remote

    if a.snap_cmd in ("ls", "show", "diff"):
        a.cmd = a.snap_cmd
        try:
            return snapmain.run_query(a, remote.STORE)
        except LookupError as e:
            die(str(e))
    h = pick_host()
    args = ["--port", a.port, "--label", a.label, "--timeout", str(a.timeout)]
    args += ["--since-last"] if a.since_last else []
    args += ["--no-regs"] if a.no_regs else []
    args += ["--smart"] if a.smart else []
    try:
        local, s = remote.take(h, HOSTS[0], a.snap_cmd, args, force_copy=a.copy)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        die(f"snap failed: {e}")
    print(
        f"a16 snap: {s['id']} via {h}: {s['collectors']} collectors in {s['elapsed_ms']} ms, failed {s['failed'] or 'none'}, "
        f"skipped {s['skipped']} (suspended devices), errors {s['errors']}, dmesg {s['dmesg_lines']} lines ({s['dmesg_errors']} err)",
        file=sys.stderr,
    )
    if "redflags" in s:  # an older a16-snap on the A16 has no red flags
        print(f"a16 snap: {snapmain.redflags_text(s['redflags'])}", file=sys.stderr)
    if not a.no_mark:
        text = f"snap {s['id']} port={a.port}: {a.label}" + (f" -- {a.mark}" if a.mark else "")
        if mark_text(text, quiet=True):
            print("a16 snap: (mark not recorded: mark socket unavailable)", file=sys.stderr)
    print(local)
    return 0


def cmd_log(a) -> int:
    sources = a.source.split(",") if a.source else ["serial", "tx", "hb", "netconsole", "mark", "obs"]
    if a.kernel:
        sources.append("kernel")
    return run_journal(sources, since=a.since, until=a.until, lines=a.lines, follow=a.follow, grep=a.grep, want=a.backend)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="a16", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="is it alive, and how")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-probe", action="store_true", help="skip the ssh port probes")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("log", help="merged timeline")
    p.add_argument("--since", default=None, help="journalctl --since, or 10m/2h/1d")
    p.add_argument("--until")
    p.add_argument("-n", "--lines", type=int)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-g", "--grep")
    p.add_argument("-k", "--kernel", action="store_true", help="add the A16 kernel log (journal upload) to the sources")
    p.add_argument(
        "--source",
        help="comma list of " + ",".join(SOURCES) + ",journal (whole A16 journal),kernel,boot,pstore,a16:IDENT",
    )
    p.add_argument("--backend", choices=["auto", "vl", "journal"], help="VictoriaLogs or journalctl (default auto)")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("redflags", help="known-bad kernel log lines (redflags.py) grouped by pattern")
    p.add_argument("--since", help="journalctl --since, or 10m/2h/1d (default: the current boot)")
    p.add_argument("--until")
    p.add_argument("--boot", nargs="?", const="current", help="a boot (id prefix; default the current one)")
    p.add_argument("--sev", choices=list(rf.SEVERITIES), default="warn", help="minimum severity (default warn)")
    p.add_argument("--source", help="comma list of kernel,netconsole,serial (default all three, deduplicated)")
    p.add_argument("-l", "--lines", action="store_true", help="also every matching line in time order")
    p.add_argument("--json", action="store_true")
    p.add_argument("--backend", choices=["auto", "vl", "journal"])
    p.set_defaults(fn=cmd_redflags)

    p = sub.add_parser("serial", help="the serial console")
    ss = p.add_subparsers(dest="action", required=True)
    q = ss.add_parser("tail")
    q.add_argument("-f", "--follow", action="store_true")
    q.add_argument("-n", "--lines", type=int, default=50)
    q.add_argument("--since")
    q.add_argument("-g", "--grep")
    q = ss.add_parser("follow")
    q.add_argument("--backlog", type=int, default=0, help="bytes of recent output first")
    ss.add_parser("status")
    for name in ("send", "break"):
        q = ss.add_parser(name)
        if name == "send":
            q.add_argument("text")
            q.add_argument("--no-cr", action="store_true", help="don't append CR")
        q.add_argument("--wait", type=float, default=3.0, help="max seconds to collect the reply")
        q.add_argument("--quiet", type=float, default=0.7, help="stop after this much silence")
        q.add_argument("--expect", help="stop once the reply matches this regex (exit 1 if it never does)")
        q.add_argument("--force", action="store_true", help="take the write lock from its holder")
    q = ss.add_parser("attach")
    q.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_serial)

    p = sub.add_parser("metrics", help="VictoriaMetrics queries; pause/resume the scrape")
    p.add_argument("name", nargs="?", help="pause|resume|state|" + "|".join(SHORTCUTS) + "|<PromQL>")
    p.add_argument("--query", "-q")
    p.add_argument("--range", "-r", help="range query over this long (1h, 30m)")
    p.add_argument("--step")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reason", help="for pause: why (goes into the mark)")
    p.set_defaults(fn=cmd_metrics)

    p = sub.add_parser("boots", help="boot history: heartbeat sink + boot records, PON dump vs a clean reboot")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--since")
    p.add_argument("--ref", help="boot id (prefix) whose PON dump is the reference (default: newest after a clean reboot)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--backend", choices=["auto", "vl", "journal"])
    p.set_defaults(fn=cmd_boots)

    p = sub.add_parser("pstore", help="pstore records shipped from the A16 (or --ssh: its directory)")
    p.add_argument("id", nargs="?")
    p.add_argument("--ssh", action="store_true", help="read the A16's /var/lib/systemd/pstore over ssh")
    p.add_argument("--backend", choices=["auto", "vl", "journal"])
    p.set_defaults(fn=cmd_pstore)

    p = sub.add_parser("mark", help="marker into the timeline")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_mark)

    from .snap import main as snapmain
    from .snap.core import topics as snap_topics

    p = sub.add_parser("snap", help="read-only diagnostic snapshot of the A16 (docs/a16-snap.md); ls/show/diff")
    ss = p.add_subparsers(dest="snap_cmd", required=True)
    snapmain.add_query_parsers(ss)
    for t in [*snap_topics(), "all"]:
        q = ss.add_parser(t, help=f"take a {t} snapshot on the A16")
        snapmain.add_take_args(q)
        q.add_argument("--mark", help="extra text for the timeline mark")
        q.add_argument("--no-mark", action="store_true", help="don't add a mark")
        q.add_argument("--copy", action="store_true", help="always run this flake's a16-snap build (nix copy), not the A16's own")
    p.set_defaults(fn=cmd_snap)

    p = sub.add_parser("ssh", help="ssh to the A16 over whichever NIC answers")
    p.add_argument("cmd", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_ssh)

    a = ap.parse_args(argv)
    try:
        sys.exit(a.fn(a) or 0)
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
