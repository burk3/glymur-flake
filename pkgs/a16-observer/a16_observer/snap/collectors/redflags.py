"""Kernel red flags in every snapshot (a16_observer/redflags.py has the list)."""

from __future__ import annotations

from ... import redflags as rf
from .. import parse
from ..core import Ctx, collector
from ..host import read_kmsg


def tally_kmsg(recs: list[dict], prev_seq: int | None) -> tuple[rf.Tally, rf.Tally, list[str]]:
    """(since boot, since prev_seq) tallies over kmsg records, and the
    warn/crit lines tagged with severity and id."""
    boot, new, lines = rf.Tally(), rf.Tally(), []
    for r in recs:
        p = rf.classify(r["msg"])
        if p is None:
            continue
        line = parse.fmt_kmsg(r)
        boot.add(line, t=r["seq"], pattern=p)
        if prev_seq is not None and r["seq"] > prev_seq:
            new.add(line, t=r["seq"], pattern=p)
        if p.sev != "info":
            lines.append(f"{p.sev:<4} {p.id:<24} {line}")
    return boot, new, lines


@collector("redflags", "kernel")
def kernel(ctx: Ctx) -> dict:
    """Known-bad kernel log lines (redflags.py): per warn/crit pattern since
    boot (count, first and last line), info counts, and counts since the
    previous snapshot of this boot; part of every topic, so `snap diff` shows
    new red flags."""
    recs = read_kmsg(ctx)
    boot, new, lines = tally_kmsg(recs, ctx.prev_seq)
    since_boot, info = {}, {}
    for h in boot.ranked("info"):
        p = h.pattern
        if p.sev == "info":  # known noise: a count only (hidden in diffs)
            info[p.id] = h.count
            continue
        since_boot[p.id] = {
            "sev": p.sev,
            "subsystem": p.subsystem,
            "bead": p.bead_id,
            "count": h.count,
            "first": h.first,
            "last": h.last,
        }
    ctx.raw("redflags", "\n".join(lines))
    top = [(h.pattern.id, h.count) for h in boot.ranked("warn")[:3]]
    return {
        "by_severity": boot.by_severity(),
        "top": dict(top),
        "since_boot": since_boot,
        "info": info,
        # None: no earlier snapshot this boot to count from
        "since_last": None if ctx.prev_seq is None else {h.pattern.id: h.count for h in new.ranked("info")},
        "since_last_by_severity": None if ctx.prev_seq is None else new.by_severity(),
        "since_seq": ctx.prev_seq,
    }
