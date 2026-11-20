"""Receivers in the a16-obs container: heartbeat, netconsole, marks.

heartbeat (UDP): datagrams "<boot_id> <uptime> [key=value ...]" (v1 is just the
first two fields). Each one is written to <run>/heartbeat.last (JSON) and,
batched, to VictoriaMetrics' import API as a16_hb_* samples stamped with the
receive time. Only events go to the journal (SYSLOG_IDENTIFIER=a16-heartbeat):
a new boot_id, state changes, a heartbeat lost (silence for 3 intervals) and
resumed.

netconsole (UDP): plain or extended (`<level>,<seq>,<usec>,<flags>;text`,
continuation lines " KEY=VALUE", userdata such as boot=<boot_id>, optionally
prefixed with the kernel release). Lines go to the journal as
SYSLOG_IDENTIFIER=a16-netconsole with A16_SEQ, A16_KTIME_USEC, A16_BOOT_ID,
A16_KERNEL_RELEASE; records already seen for that boot (the sender's ring
buffer replay, A16_REPLAY=1) are dropped, live forward jumps are logged.

marks (unix stream socket): one JSON line {"text": ..., "holder": ...} per
connection, logged as SYSLOG_IDENTIFIER=a16-mark with the peer's uid.
"""

from __future__ import annotations

import asyncio
import bisect
import collections
import grp
import json
import os
import re
import threading
import time
import urllib.request

from .journal import INFO, NOTICE, WARNING, Journal
from .serial import peer_uid

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NUM_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$")


# --- heartbeat ---------------------------------------------------------------
def parse_heartbeat(data: bytes) -> dict | None:
    """Parse one heartbeat datagram; None if it isn't one."""
    try:
        text = data.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    parts = text.split()
    if len(parts) < 2 or not UUID_RE.match(parts[0]):
        return None
    try:
        uptime = float(parts[1])
    except ValueError:
        return None
    hb = {"boot_id": parts[0], "uptime": uptime, "fields": {}}
    for p in parts[2:]:
        k, sep, v = p.partition("=")
        if not sep or not KEY_RE.match(k) or len(v) > 64:
            continue
        hb["fields"][k] = v
    return hb


def _label(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def heartbeat_samples(hb: dict, src: str, t: float, host: str = "zenbook-a16") -> list[str]:
    """Prometheus text-format lines (with ms timestamps) for one heartbeat."""
    ms = int(t * 1000)
    base = f'host="{_label(host)}",src="{_label(src)}"'
    out = [f"a16_hb_uptime_seconds{{{base}}} {hb['uptime']} {ms}"]
    f = hb["fields"]
    for k, v in sorted(f.items()):
        if k in ("v", "state") or not NUM_RE.match(v):
            continue
        out.append(f"a16_hb_{k}{{{base}}} {v} {ms}")
    if "state" in f:
        out.append(f'a16_hb_state{{{base},state="{_label(f["state"])}"}} 1 {ms}')
    out.append(f'a16_hb_boot_info{{{base},boot_id="{hb["boot_id"]}"}} 1 {ms}')
    return out


class MetricsBridge:
    """Batches samples and POSTs them to VictoriaMetrics' import API in a thread."""

    def __init__(self, url: str | None, journal: Journal, interval: float = 5.0, max_lines: int = 20000):
        self.url = url
        self.journal = journal
        self.interval = interval
        self.max_lines = max_lines
        self.lines: collections.deque[str] = collections.deque(maxlen=max_lines)
        self.lock = threading.Lock()
        self.failing = False

    def add(self, lines: list[str]) -> None:
        if not self.url:
            return
        with self.lock:
            self.lines.extend(lines)

    def flush(self) -> None:
        with self.lock:
            batch = list(self.lines)
            self.lines.clear()
        if not batch:
            return
        body = ("\n".join(batch) + "\n").encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            if self.failing:
                self.journal.send(f"metrics import to {self.url} works again", NOTICE)
            self.failing = False
        except Exception as e:  # noqa: BLE001 - keep the sink running whatever happens
            if not self.failing:
                self.journal.send(f"metrics import to {self.url} failed: {e}; buffering", WARNING)
            self.failing = True
            with self.lock:
                # Put the batch back in front, oldest dropped by the deque bound.
                self.lines.extendleft(reversed(batch))

    async def run(self) -> None:
        if not self.url:
            return
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self.interval)
            await loop.run_in_executor(None, self.flush)


class HeartbeatSink(asyncio.DatagramProtocol):
    def __init__(self, run_dir: str, bridge: MetricsBridge, journal: Journal, lost_after: float = 3.0):
        self.run_dir = run_dir
        self.bridge = bridge
        self.journal = journal
        self.lost_after = lost_after  # in heartbeat intervals
        self.last: dict | None = None
        self.lost = False
        self.bad = 0
        self.bad_logged = 0.0

    def interval(self) -> float:
        try:
            return max(1.0, float(self.last["fields"].get("iv", 1)))
        except (TypeError, ValueError):
            return 1.0

    def datagram_received(self, data, addr) -> None:
        self.receive(data, addr[0], time.time())

    def receive(self, data: bytes, src: str, now: float) -> None:
        hb = parse_heartbeat(data)
        if hb is None:
            self.bad += 1
            if now - self.bad_logged > 60:
                self.journal.send(f"ignoring malformed heartbeat from {src} ({self.bad} so far)", WARNING)
                self.bad_logged = now
            return
        hb["t"] = now
        hb["src"] = src
        prev = self.last
        state = hb["fields"].get("state")
        ev = dict(A16_BOOT_ID=hb["boot_id"], A16_UPTIME=hb["uptime"], A16_SRC=src, A16_STATE=state)
        if prev is None or prev["boot_id"] != hb["boot_id"]:
            if prev is None:
                msg = f"heartbeat from boot {hb['boot_id']} (uptime {hb['uptime']:.1f}s) via {src}"
            else:
                msg = (
                    f"new boot {hb['boot_id']} (uptime {hb['uptime']:.1f}s) via {src}; previous boot "
                    f"{prev['boot_id']} last seen at uptime {prev['uptime']:.1f}s, "
                    f"state {prev['fields'].get('state', '?')}, {now - prev['t']:.1f}s ago"
                )
            self.journal.send(
                msg,
                NOTICE,
                A16_EVENT="boot",
                A16_PREV_BOOT_ID=prev["boot_id"] if prev else None,
                A16_PREV_UPTIME=prev["uptime"] if prev else None,
                A16_PREV_STATE=prev["fields"].get("state") if prev else None,
                **ev,
            )
        else:
            if self.lost:
                self.journal.send(
                    f"heartbeat resumed after {now - prev['t']:.1f}s silence (uptime {hb['uptime']:.1f}s, "
                    f"state {state or '?'}) via {src}",
                    NOTICE,
                    A16_EVENT="resumed",
                    A16_GAP=round(now - prev["t"], 3),
                    **ev,
                )
            prev_state = prev["fields"].get("state")
            if state != prev_state and state is not None:
                self.journal.send(
                    f"state {prev_state or '?'} -> {state} (uptime {hb['uptime']:.1f}s, "
                    f"suspends {hb['fields'].get('susp', '?')})",
                    INFO,
                    A16_EVENT="state",
                    **ev,
                )
            if prev["src"] != src:
                self.journal.send(f"heartbeats now arrive via {src} (was {prev['src']})", INFO, A16_EVENT="route", **ev)
        self.lost = False
        self.last = hb
        self.write_last(hb)
        self.bridge.add(heartbeat_samples(hb, src, now))

    def write_last(self, hb: dict) -> None:
        rec = {
            "t": hb["t"],
            "src": hb["src"],
            "boot_id": hb["boot_id"],
            "uptime": hb["uptime"],
            **hb["fields"],
        }
        path = os.path.join(self.run_dir, "heartbeat.last")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
            f.write("\n")
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)

    def check(self, now: float) -> None:
        if self.last is None or self.lost:
            return
        silent = now - self.last["t"]
        if silent > self.lost_after * self.interval():
            st = self.last["fields"].get("state", "?")
            self.journal.send(
                f"heartbeat lost after uptime {self.last['uptime']:.1f}s (last state {st}, "
                f"boot {self.last['boot_id']}, via {self.last['src']})",
                WARNING if st not in ("suspending",) else NOTICE,
                A16_EVENT="lost",
                A16_BOOT_ID=self.last["boot_id"],
                A16_UPTIME=self.last["uptime"],
                A16_STATE=st,
            )
            self.lost = True

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            self.check(time.time())


async def run_heartbeat(args) -> None:
    journal = Journal("a16-heartbeat")
    bridge = MetricsBridge(args.import_url or None, journal)
    sink = HeartbeatSink(args.run_dir, bridge, journal)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: sink, local_addr=(args.listen, args.port), reuse_port=False)
    journal.send(f"heartbeat sink on {args.listen}:{args.port}, importing to {args.import_url or 'nowhere'}", INFO)
    await asyncio.gather(sink.watchdog(), bridge.run())


# --- netconsole --------------------------------------------------------------
EXT_RE = re.compile(rb"^(\d+),(\d+),(\d+),([^;]*);")
# With the target's `release` attribute set, the kernel prepends its release:
# "7.3.0,6,1234,5678,-;text".
REL_RE = re.compile(rb"^([0-9A-Za-z][0-9A-Za-z._+~-]{0,63}),(?=\d+,\d+,\d+,[^;]*;)")


def parse_netconsole(data: bytes) -> list[dict]:
    """Records in one netconsole datagram.

    Extended: [{"level", "facility", "seq", "usec", "flags", "text", "meta",
    "release"}], meta being the " KEY=VALUE" continuation lines (kernel dict
    entries and the target's userdata, e.g. boot=<boot_id>). Plain: one record
    per non-empty line, {"text": ...}.
    """
    release = None
    rm = REL_RE.match(data)
    if rm:
        release = rm.group(1).decode("ascii", "replace")
        data = data[rm.end() :]
    m = EXT_RE.match(data)
    if m:
        rest = data[m.end() :]
        lines = rest.split(b"\n")
        text = lines[0].decode("utf-8", "backslashreplace")
        meta = {}
        for ln in lines[1:]:
            if ln.startswith(b" ") and b"=" in ln:
                k, _, v = ln[1:].partition(b"=")
                meta[k.decode("ascii", "replace")] = v.decode("utf-8", "backslashreplace")
        flags = {}
        for f in m.group(4).split(b","):
            k, _, v = f.partition(b"=")
            flags[k.decode("ascii", "replace")] = v.decode("ascii", "replace")
        prio = int(m.group(1))
        r = {
            "level": prio & 7,
            "facility": prio >> 3,
            "seq": int(m.group(2)),
            "usec": int(m.group(3)),
            "flags": flags,
            "text": text,
            "meta": meta,
        }
        if release is not None:
            r["release"] = release
        return [r]
    out = []
    for ln in data.split(b"\n"):
        ln = ln.rstrip(b"\r")
        if ln.strip():
            out.append({"text": ln.decode("utf-8", "backslashreplace")})
    return out


class SeqSet:
    """Set of sequence numbers as sorted, merged [lo, hi] ranges.

    A boot's kernel log is a few contiguous runs (live stream, replays of the
    ring buffer), so this stays a handful of ranges however many records
    arrive, and every replay can be deduplicated against everything seen.
    """

    def __init__(self):
        self.r: list[list[int]] = []

    def __contains__(self, x: int) -> bool:
        i = bisect.bisect_right(self.r, [x, float("inf")]) - 1
        return i >= 0 and self.r[i][0] <= x <= self.r[i][1]

    def add(self, x: int) -> bool:
        """Add x; False if it was already there."""
        r = self.r
        i = bisect.bisect_right(r, [x, float("inf")]) - 1
        if i >= 0 and r[i][0] <= x <= r[i][1]:
            return False
        if i >= 0 and r[i][1] == x - 1:
            r[i][1] = x
            if i + 1 < len(r) and r[i + 1][0] == x + 1:
                r[i][1] = r[i + 1][1]
                del r[i + 1]
        elif i + 1 < len(r) and r[i + 1][0] == x + 1:
            r[i + 1][0] = x
        else:
            r.insert(i + 1, [x, x])
        return True


class NetconsoleSink(asyncio.DatagramProtocol):
    """Extended records are keyed by boot (userdata boot=, else the source
    address) and (seq, fragment offset): duplicates, from the kernel log
    replay that a16-telemetry sends after attaching, are dropped. A forward
    jump in seq on the live stream is logged as a gap."""

    def __init__(self, journal: Journal, boots: int = 16):
        self.journal = journal
        self.boots = boots
        self.seen: collections.OrderedDict[str, SeqSet] = collections.OrderedDict()
        self.frags: collections.OrderedDict[tuple, bool] = collections.OrderedDict()
        self.last_seq: dict[str, int] = {}

    def datagram_received(self, data, addr) -> None:
        self.receive(data, addr[0])

    def _seen(self, key: str) -> SeqSet:
        if key not in self.seen:
            self.seen[key] = SeqSet()
            while len(self.seen) > self.boots:
                old, _ = self.seen.popitem(last=False)
                self.last_seq.pop(old, None)
        self.seen.move_to_end(key)
        return self.seen[key]

    def receive(self, data: bytes, src: str) -> None:
        for r in parse_netconsole(data):
            if "seq" not in r:
                self.journal.send(r["text"], INFO, A16_SRC=src)
                continue
            boot = r["meta"].get("boot") or r["meta"].get("boot_id")
            key = boot or src
            seq = r["seq"]
            frag = r["flags"].get("ncfrag")
            replay = r["meta"].get("replay") == "1"
            if frag and not frag.startswith("0/"):
                # Later fragments of a long record share its seq.
                fk = (key, seq, frag)
                if fk in self.frags:
                    continue
                self.frags[fk] = True
                while len(self.frags) > 4096:
                    self.frags.popitem(last=False)
            elif not self._seen(key).add(seq):
                continue
            last = self.last_seq.get(key)
            if not replay and last is not None and seq > last + 1:
                self.journal.send(
                    f"netconsole gap: {seq - last - 1} message(s) missing (seq {last + 1}..{seq - 1})",
                    WARNING,
                    A16_EVENT="gap",
                    A16_SRC=src,
                    A16_BOOT_ID=boot,
                )
            if not replay and (last is None or seq > last):
                self.last_seq[key] = seq
            self.journal.send(
                r["text"],
                r["level"],
                A16_SEQ=seq,
                A16_KTIME_USEC=r["usec"],
                A16_BOOT_ID=boot,
                A16_SRC=src,
                A16_NCFRAG=frag,
                A16_KERNEL_RELEASE=r.get("release"),
                A16_REPLAY="1" if replay else None,
            )


async def run_netconsole(args) -> None:
    journal = Journal("a16-netconsole")
    sink = NetconsoleSink(journal)
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: sink, local_addr=(args.listen, args.port))
    journal.send(f"netconsole sink on {args.listen}:{args.port}", INFO, identifier="a16-obs")
    await asyncio.Event().wait()


# --- marks -------------------------------------------------------------------
async def run_marks(args) -> None:
    journal = Journal("a16-mark")

    async def handle(reader, writer):
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock) if sock is not None else None
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            obj = json.loads(line or b"{}")
            text = str(obj.get("text", "")).strip()[:4000]
            if not text:
                raise ValueError("empty mark")
            holder = str(obj.get("holder") or "")[:64]
            journal.send(text, NOTICE, A16_HOLDER=holder, A16_CLIENT_UID=uid)
            writer.write(b'{"ok": true}\n')
        except (ValueError, asyncio.TimeoutError, AttributeError) as e:
            writer.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    try:
        os.unlink(args.socket)
    except FileNotFoundError:
        pass
    old = os.umask(0o117)
    try:
        server = await asyncio.start_unix_server(handle, path=args.socket)
    finally:
        os.umask(old)
    if args.group:
        os.chown(args.socket, -1, grp.getgrnam(args.group).gr_gid)
    os.chmod(args.socket, 0o660)
    async with server:
        await server.serve_forever()

