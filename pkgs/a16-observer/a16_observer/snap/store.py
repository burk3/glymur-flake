"""Snapshot store layout, shared by the A16 (/var/lib/a16-snap) and the observer host
(~/a16-snaps):

  <store>/index.jsonl                      one line per snapshot (key fields)
  <store>/cursor.json                      A16 only: kernel log cursor for --since-last
  <store>/<topic>/<UTC ts>_<label-slug>/   one snapshot
      snapshot.json   structured (schema below, docs/a16-snap.md)
      raw/*.txt       verbatim command outputs and files
      dmesg.txt       kernel log since boot, or since the last snapshot

A snapshot id is "<topic>/<ts>_<slug>"."""

from __future__ import annotations

import json
import os
import re
import shutil
import time

INDEX_FIELDS = ("id", "topic", "label", "port", "taken_at", "host", "boot_id", "generation", "kernel", "n_errors", "n_skipped", "failed")


def slug(label: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", label or "").strip("-").lower()
    return s[:60] or "snap"


def new_id(topic: str, label: str, now: float | None = None) -> str:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    return f"{topic}/{ts}_{slug(label)}"


def index_entry(snap: dict) -> dict:
    e = {k: snap.get(k) for k in INDEX_FIELDS if k not in ("n_errors", "n_skipped", "failed")}
    e["n_errors"] = len(snap.get("errors", []))
    e["n_skipped"] = len(snap.get("skipped", []))
    e["failed"] = sorted(k for k, v in snap.get("collectors", {}).items() if not v.get("ok"))
    return e


def write(store: str, snap: dict, raws: dict[str, str], dmesg: str) -> str:
    base, n = snap["id"], 2
    d = os.path.join(store, base)
    while os.path.exists(d):  # two snapshots within a second
        snap["id"] = f"{base}-{n}"
        d = os.path.join(store, snap["id"])
        n += 1
    os.makedirs(os.path.join(d, "raw"), exist_ok=True)
    for name, text in sorted(raws.items()):
        with open(os.path.join(d, "raw", f"{name}.txt"), "w") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    with open(os.path.join(d, "dmesg.txt"), "w") as f:
        f.write(dmesg)
    tmp = os.path.join(d, ".snapshot.json.tmp")
    with open(tmp, "w") as f:
        json.dump(snap, f, indent=1, sort_keys=False, default=str)
        f.write("\n")
    os.replace(tmp, os.path.join(d, "snapshot.json"))
    append_index(store, index_entry(snap))
    return d


def append_index(store: str, entry: dict) -> None:
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "index.jsonl"), "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def entries(store: str) -> list[dict]:
    """Index lines whose snapshot directory still exists, oldest first (the
    directory scan fills in snapshots missing from the index)."""
    seen: dict[str, dict] = {}
    try:
        with open(os.path.join(store, "index.jsonl")) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("id"):
                    seen[e["id"]] = e
    except OSError:
        pass
    for sj in _scan(store):
        sid = os.path.relpath(os.path.dirname(sj), store)
        if sid not in seen:
            try:
                with open(sj) as f:
                    seen[sid] = index_entry(json.load(f)) | {"id": sid}
            except (OSError, ValueError):
                continue
    out = [e for e in seen.values() if os.path.isfile(os.path.join(store, e["id"], "snapshot.json"))]
    return sorted(out, key=lambda e: (e.get("taken_at") or "", e["id"]))


def _scan(store: str) -> list[str]:
    out = []
    try:
        for topic in os.listdir(store):
            td = os.path.join(store, topic)
            if not os.path.isdir(td):
                continue
            for name in os.listdir(td):
                sj = os.path.join(td, name, "snapshot.json")
                if os.path.isfile(sj):
                    out.append(sj)
    except OSError:
        pass
    return out


def resolve(store: str, ref: str, topic: str | None = None) -> str:
    """A snapshot reference -> its directory. Accepts a directory path, an id
    ("usb/20261027T101500Z_boot"), the directory name, a unique substring, or
    @-N (N-th newest, optionally within --topic)."""
    if os.path.isfile(os.path.join(ref, "snapshot.json")):
        return os.path.abspath(ref)
    es = entries(store)
    if topic:
        es = [e for e in es if e.get("topic") == topic]
    m = re.fullmatch(r"@-?(\d+)", ref)
    if m:
        n = int(m.group(1)) or 1
        if n > len(es):
            raise LookupError(f"{ref}: only {len(es)} snapshots")
        return os.path.join(store, es[-n]["id"])
    for e in es:
        if e["id"] == ref or e["id"].split("/", 1)[-1] == ref:
            return os.path.join(store, e["id"])
    hits = [e for e in es if e["id"].endswith("_" + ref)]  # the label slug
    if len(hits) == 1:
        return os.path.join(store, hits[0]["id"])
    hits = hits or [e for e in es if ref in e["id"]]
    if len(hits) == 1:
        return os.path.join(store, hits[0]["id"])
    if not hits:
        raise LookupError(f"no snapshot matches {ref!r}")
    raise LookupError(f"{ref!r} is ambiguous: " + ", ".join(e["id"] for e in hits[:8]))


def load(d: str) -> dict:
    with open(os.path.join(d, "snapshot.json")) as f:
        return json.load(f)


def prune(store: str, keep: int) -> list[str]:
    """Deletes the oldest snapshot directories beyond `keep` (A16 side)."""
    es = entries(store)
    gone = []
    for e in es[: max(0, len(es) - keep)]:
        d = os.path.join(store, e["id"])
        if os.path.realpath(d).startswith(os.path.realpath(store) + os.sep):
            shutil.rmtree(d, ignore_errors=True)
            gone.append(e["id"])
    return gone
