"""a16-snap: take a read-only diagnostic snapshot on the A16 (docs/a16-snap.md).

  a16-snap TOPIC [--port c1|c2|a1|all] --label TEXT [--since-last]
               [--no-regs] [--smart] [--timeout S] [--store DIR] [--keep N] [--json]
  a16-snap ls [TOPIC] [--label RE] [-n N]
  a16-snap show ID [--raw NAME]
  a16-snap diff ID1 ID2 [--all] [-g RE] [--json]
  a16-snap topics

TOPIC: usb display suspend audio net power redflags all (the kernel red
flags collector runs with every topic). Prints the snapshot directory
(or, with --json, a one-line summary) on stdout. Reads only: see "Safety" in
docs/a16-snap.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from . import SNAP_VERSION, collectors  # noqa: F401  (registers the collectors)
from . import diff as snapdiff
from . import host, parse, store
from .core import COLLECTORS, PORTS, SCHEMA_VERSION, Ctx, run_collectors, topics

DEFAULT_STORE = os.environ.get("A16_SNAP_STORE", "/var/lib/a16-snap")


def take(ctx: Ctx, topic: str, label: str, since_last: bool, store_dir: str, keep: int) -> tuple[str, dict]:
    t0 = time.monotonic()
    now = time.time()
    ident = host.identity(ctx)
    cursor_path = os.path.join(store_dir, "cursor.json")
    cursor = {}
    try:
        with open(cursor_path) as f:
            cursor = json.load(f)
    except (OSError, ValueError):
        pass
    if cursor.get("boot_id") == ident["boot_id"] and cursor.get("seq") is not None:
        ctx.prev_seq = cursor["seq"]  # red flags since the previous snapshot
    results = run_collectors(ctx, topic)
    recs = host.read_kmsg(ctx)

    mode, from_seq = "since_boot", None
    if since_last:
        if cursor.get("boot_id") == ident["boot_id"] and cursor.get("seq") is not None:
            mode, from_seq = "since_last", cursor["seq"]
        else:
            mode = "since_boot (no earlier snapshot this boot)"
    window = [r for r in recs if from_seq is None or r["seq"] > from_seq]
    dmesg = "".join(parse.fmt_kmsg(r) + "\n" for r in window)

    snap = {
        "schema": SCHEMA_VERSION,
        "tool": "a16-snap",
        "version": SNAP_VERSION,
        "id": store.new_id(topic, label, now),
        "topic": topic,
        "label": label,
        "port": ctx.port,
        "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "host": ident["host"],
        "boot_id": ident["boot_id"],
        "generation": ident["generation"],
        "kernel": ident["kernel"],
        "identity": ident,
        "options": {"regs": ctx.regs, "smart": ctx.smart, "since_last": since_last, "timeout_s": ctx.timeout},
        "dmesg": {
            "mode": mode,
            "from_seq": from_seq,
            "to_seq": recs[-1]["seq"] if recs else None,
            "lines": len(window),
            "levels_le_3": sum(1 for r in window if r["level"] <= 3),
        },
        "collectors": results,
        "skipped": ctx.skipped,
        "errors": ctx.errors,
    }
    snap["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
    snap["raw_files"] = sorted(ctx.raws)
    d = store.write(store_dir, snap, ctx.raws, dmesg)
    if recs:
        tmp = cursor_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"boot_id": ident["boot_id"], "seq": recs[-1]["seq"], "id": snap["id"]}, f)
        os.replace(tmp, cursor_path)
    if keep > 0:
        store.prune(store_dir, keep)
    return d, snap


def summary(d: str, snap: dict) -> dict:
    return {
        "id": snap["id"],
        "dir": d,
        "topic": snap["topic"],
        "port": snap["port"],
        "label": snap["label"],
        "generation": snap["generation"],
        "boot_id": snap["boot_id"],
        "elapsed_ms": snap["elapsed_ms"],
        "collectors": len(snap["collectors"]),
        "failed": [k for k, v in snap["collectors"].items() if not v.get("ok")],
        "skipped": len(snap["skipped"]),
        "errors": len(snap["errors"]),
        "dmesg_lines": snap["dmesg"]["lines"],
        "dmesg_errors": snap["dmesg"]["levels_le_3"],
        "redflags": redflags_summary(snap),
        "version": snap["version"],
    }


def redflags_summary(snap: dict) -> dict | None:
    """{"boot": {sev: n}, "new": {sev: n} or None, "top": {id: n}} from the
    redflags collector, or None if it did not run."""
    res = (snap.get("collectors") or {}).get("redflags.kernel") or {}
    if not res.get("ok"):
        return None
    d = res["data"]
    return {"boot": d["by_severity"], "new": d.get("since_last_by_severity"), "top": d.get("top", {})}


def redflags_text(rs: dict | None) -> str:
    """'red flags since boot: crit 3, warn 12, info 40 (new: crit 1); top: ...'"""
    if not rs:
        return "red flags: not collected"
    s = "red flags since boot: " + ", ".join(f"{k} {v}" for k, v in rs["boot"].items())
    if rs.get("new") is not None:
        new = ", ".join(f"{k} {v}" for k, v in rs["new"].items() if v and k != "info")
        s += f" (since the last snapshot: {new or 'none'})"
    if rs.get("top"):
        s += "; top: " + ", ".join(f"{k} {v}" for k, v in rs["top"].items())
    return s


# --- ls / show / diff (shared with the observer host's `a16 snap`) -------------------------
def cmd_ls(store_dir: str, topic: str | None, label_re: str | None, n: int) -> int:
    es = store.entries(store_dir)
    if topic:
        es = [e for e in es if e.get("topic") == topic]
    if label_re:
        rx = re.compile(label_re, re.I)
        es = [e for e in es if rx.search(e.get("label") or "")]
    for e in es[-n:] if n else es:
        flags = []
        if e.get("failed"):
            flags.append(f"failed:{','.join(e['failed'])}")
        if e.get("n_errors"):
            flags.append(f"errors:{e['n_errors']}")
        print(f"{e['id']:<58} port={e.get('port') or '-':<4} gen={e.get('generation')} boot={str(e.get('boot_id'))[:8]}  {e.get('label')!r} {' '.join(flags)}")
    if not es:
        print(f"(no snapshots in {store_dir})", file=sys.stderr)
    return 0


def cmd_show(store_dir: str, ref: str, raw: str | None, as_json: bool) -> int:
    d = store.resolve(store_dir, ref)
    if raw:
        p = os.path.join(d, "raw", raw if raw.endswith(".txt") else raw + ".txt")
        if raw == "dmesg":
            p = os.path.join(d, "dmesg.txt")
        with open(p) as f:
            sys.stdout.write(f.read())
        return 0
    snap = store.load(d)
    if as_json:
        json.dump(snap, sys.stdout, indent=1)
        print()
        return 0
    print(f"{snap['id']}  ({d})")
    print(f"  label {snap['label']!r}  port {snap['port']}  taken {snap['taken_at']}  {snap['elapsed_ms']} ms")
    print(f"  host {snap['host']}  gen {snap['generation']}  kernel {snap['kernel']}  boot {snap['boot_id']}")
    dm = snap["dmesg"]
    print(f"  dmesg {dm['mode']}: {dm['lines']} lines, {dm['levels_le_3']} at err or worse")
    print(f"  {redflags_text(redflags_summary(snap))}")
    for name, res in snap["collectors"].items():
        st = "ok" if res.get("ok") else f"FAILED {res.get('error')}"
        print(f"  {name:<24} {res.get('elapsed_ms', 0):>6} ms  {st}")
    for s in snap["skipped"]:
        print(f"  skipped {s.get('path') or s.get('what')}: {s['reason']}")
    for e in snap["errors"]:
        print(f"  error {e.get('path') or e.get('cmd')}: {e['error']}")
    print(f"  raw/: {', '.join(snap.get('raw_files', []))}")
    flat = snapdiff.snapshot_paths(snap)
    print()
    for k, v in flat.items():
        s = repr(v) if isinstance(v, str) else str(v)
        print(f"{k} = {s if len(s) < 160 else s[:157] + '...'}")
    return 0


def cmd_diff(store_dir: str, a: str, b: str, include_volatile: bool, grep: str | None, as_json: bool) -> int:
    sa = store.load(store.resolve(store_dir, a))
    sb = store.load(store.resolve(store_dir, b))
    d = snapdiff.diff(sa, sb, include_volatile=include_volatile, grep=grep)
    if as_json:
        json.dump(d, sys.stdout, indent=1, default=str)
        print()
    else:
        print(snapdiff.format_diff(d))
    return 0


def add_query_parsers(sub) -> None:
    p = sub.add_parser("ls", help="list snapshots")
    p.add_argument("topic", nargs="?")
    p.add_argument("--label", help="regex on the label")
    p.add_argument("-n", type=int, default=0, help="newest N only")
    p = sub.add_parser("show", help="one snapshot: summary + flattened fields, or --raw NAME")
    p.add_argument("id")
    p.add_argument("--raw", help="a raw/ file name (or 'dmesg')")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("diff", help="structured diff of two snapshots' snapshot.json")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--all", action="store_true", help="include volatile fields (PM clocks, uptime)")
    p.add_argument("-g", "--grep", help="only paths matching this regex")
    p.add_argument("--json", action="store_true")


def run_query(a, store_dir: str) -> int | None:
    if a.cmd == "ls":
        return cmd_ls(store_dir, a.topic, a.label, a.n)
    if a.cmd == "show":
        return cmd_show(store_dir, a.id, a.raw, a.json)
    if a.cmd == "diff":
        return cmd_diff(store_dir, a.a, a.b, a.all, a.grep, a.json)
    return None


def add_take_args(p) -> None:
    p.add_argument("--port", default="all", choices=[*PORTS, "all"])
    p.add_argument("--label", "-l", required=True, help="what this snapshot is (e.g. 'c2 boot-present')")
    p.add_argument("--since-last", action="store_true", help="dmesg.txt from the previous snapshot's cursor (this boot)")
    p.add_argument("--no-regs", action="store_true", help="skip register-backed debugfs even on active devices")
    p.add_argument("--smart", action="store_true", help="smartctl -i -H on disks on the port")
    p.add_argument("--timeout", type=float, default=5.0, help="per read/command timeout, seconds")


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="a16-snap", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--version", action="version", version=f"a16-snap {SNAP_VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_query_parsers(sub)
    sub.add_parser("topics", help="topics and their collectors")
    for t in [*topics(), "all"]:
        p = sub.add_parser(t, help=f"take a {t} snapshot")
        add_take_args(p)
        p.add_argument("--keep", type=int, default=100, help="snapshots kept in the store (oldest pruned)")
        p.add_argument("--json", action="store_true", help="print a one-line JSON summary instead of the directory")
    a = ap.parse_args(argv)
    try:
        rc = run_query(a, a.store)
    except LookupError as e:
        print(f"a16-snap: {e}", file=sys.stderr)
        sys.exit(1)
    except BrokenPipeError:
        os._exit(0)
    if rc is not None:
        sys.exit(rc)
    if a.cmd == "topics":
        for t in topics():
            print(t)
            for c in COLLECTORS:
                if c.topic == t:
                    print(f"  {c.name:<16} {c.doc}")
        return
    if os.geteuid() != 0 and not os.environ.get("A16_SNAP_ALLOW_USER"):
        print("a16-snap: run as root (debugfs and /dev/kmsg)", file=sys.stderr)
        sys.exit(2)
    ctx = Ctx(timeout=a.timeout, port=a.port, regs=not a.no_regs, smart=a.smart)
    os.umask(0o077)  # raw dumps include the kernel log; root only
    os.makedirs(a.store, exist_ok=True)
    d, snap = take(ctx, a.cmd, a.label, a.since_last, a.store, a.keep)
    s = summary(d, snap)
    if a.json:
        print(json.dumps(s))
    else:
        print(f"a16-snap: {s['id']}: {s['collectors']} collectors in {s['elapsed_ms']} ms, failed {s['failed'] or 'none'}, skipped {s['skipped']}, errors {s['errors']}", file=sys.stderr)
        print(f"a16-snap: {redflags_text(s['redflags'])}", file=sys.stderr)
        print(d)
    # Abandoned reader threads (a read that hit the timeout) must not hold the
    # process open.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
