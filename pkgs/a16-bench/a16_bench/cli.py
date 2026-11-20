"""a16-bench command line. See docs/benchmarks.md."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import compare as cmp
from . import meta as metamod
from .benchmarks import REGISTRY
from .runner import DEFAULT_ROOT, PROFILES, plan, run_profile
from .sysfs import Probe


def _csv(s: str) -> list[str]:
    return [x for x in s.split(",") if x]


def _param(s: str) -> tuple[str, str, object]:
    """test.key=value (value parsed as JSON when it can be)."""
    k, sep, v = s.partition("=")
    test, dot, key = k.partition(".")
    if not sep or not dot:
        raise argparse.ArgumentTypeError("expected TEST.KEY=VALUE, e.g. idle-power.seconds=120")
    try:
        val = json.loads(v)
    except ValueError:
        val = v
    return test, key, val


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="a16-bench", description="Milestone benchmarks for the Zenbook A16 (docs/benchmarks.md).")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--out", default=str(DEFAULT_ROOT), help=f"results root (default {DEFAULT_ROOT})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a profile")
    r.add_argument("profile", nargs="?", default="quick", choices=list(PROFILES))
    r.add_argument("--label", help="run label (default PROFILE-genN)")
    r.add_argument("--only", type=_csv, help="comma-separated tests to run (in this order)")
    r.add_argument("--skip", type=_csv, help="comma-separated tests to leave out")
    r.add_argument("--param", type=_param, action="append", default=[], metavar="TEST.KEY=VALUE", help="override a test parameter")
    r.add_argument("--allow-allcore", action="store_true", help="run all-core CPU loads (cpu-multi); see bead baw")
    r.add_argument("--allow-session", action="store_true", help="run even though a desktop session is active")
    r.add_argument("--iperf-server", default="lofty305.lan")
    r.add_argument("--s2idle-manual", action="store_true", help="s2idle without an RTC alarm: a human wakes the A16")
    r.add_argument("--scratch-dir", help="directory for the fio file (default: the results root)")
    r.add_argument("--cooldown-c", type=float, default=50.0, help="wait until the hottest die zone is at or below this before each test")
    r.add_argument("--cooldown-timeout", type=float, default=240.0)
    r.add_argument("--no-cooldown", action="store_true")
    r.add_argument("--abort-c", type=float, default=105.0, help="stop a workload when a die zone reaches this")
    r.add_argument("--dry-run", action="store_true", help="show the plan and what would be skipped")

    c = sub.add_parser("compare", help="compare two runs (ids, unique id prefixes/suffixes, dirs or results.json)")
    c.add_argument("a")
    c.add_argument("b")
    c.add_argument("--threshold", type=float, default=5.0, help="percent change that counts (default 5)")
    c.add_argument("--all", action="store_true", help="all metrics, not only headline ones")
    c.add_argument("--json", action="store_true")

    sub.add_parser("runs", help="list runs under the results root")
    s = sub.add_parser("show", help="print a run's headline results")
    s.add_argument("run")
    sub.add_parser("tests", help="list tests")
    sub.add_parser("profiles", help="list profiles")
    sub.add_parser("meta", help="print the metadata a run would record")

    args = ap.parse_args(argv)
    root = Path(args.out)

    if args.cmd == "run":
        params: dict[str, dict[str, object]] = {}
        for test, key, val in args.param:
            params.setdefault(test, {})[key] = val
        args.param = params
        args.argv = argv
        if args.dry_run:
            return dry_run(args)
        run_profile(args)
        return 0
    if args.cmd == "compare":
        res = cmp.compare(cmp.load(args.a, root), cmp.load(args.b, root), args.threshold, args.all)
        print(json.dumps(res, indent=1) if args.json else cmp.format_text(res))
        return 0
    if args.cmd == "runs":
        for d in sorted(root.glob("*")):
            f = d / "results.json"
            if not f.is_file():
                continue
            doc = json.loads(f.read_text())
            st = [t.get("status") for t in doc.get("tests", {}).values()]
            flag = "" if doc.get("complete") else "  INCOMPLETE"
            print(f"{d.name}  {doc.get('meta', {}).get('kernel', {}).get('release')}  {doc.get('meta', {}).get('power', {}).get('source')}  ok={st.count('ok')} skipped={st.count('skipped')} failed={st.count('failed') + st.count('aborted')}{flag}")
        return 0
    if args.cmd == "show":
        doc = cmp.load(args.run, root)
        print(f"{doc['run_id']} ({doc.get('profile')}, complete={doc.get('complete')})")
        for name, t in doc.get("tests", {}).items():
            print(f"  {name}: {t.get('status')}" + (f" [{t.get('reason')}]" if t.get("reason") else ""))
            for k, v in (t.get("metrics") or {}).items():
                if v.get("headline"):
                    print(f"      {k} = {v['value']} {v['unit']}")
        return 0
    if args.cmd == "tests":
        for b in REGISTRY.values():
            flags = [f for f, on in (("allcore", b.allcore), ("root", b.root), (f"power={b.power}", b.power != "any")) if on]
            print(f"{b.name:<14} ~{b.est_s:>4}s  {b.title}" + (f"  [{', '.join(flags)}]" if flags else ""))
        return 0
    if args.cmd == "profiles":
        for n, p in PROFILES.items():
            est = sum(REGISTRY[t].est_s for t, _ in p["tests"])
            print(f"{n:<7} ~{est // 60:>2} min  {p['about']}\n        {', '.join(t for t, _ in p['tests'])}")
        return 0
    if args.cmd == "meta":
        print(json.dumps(metamod.collect(Probe()), indent=1))
        return 0
    return 1


def dry_run(args) -> int:
    probe = Probe()
    opts = {"allow_allcore": args.allow_allcore, "s2idle_manual": args.s2idle_manual}
    total = 0
    print(f"profile {args.profile}, power {probe.power_source()['source']}")
    for name, overrides in plan(args.profile, args.only, args.skip):
        b = REGISTRY[name]
        reason = b.precheck(opts, probe)
        if name == "s2idle" and not reason and not probe.wakealarm_rtc() and not args.s2idle_manual:
            reason = "no RTC wake alarm; needs --s2idle-manual"
        params = {**b.params, **overrides, **args.param.get(name, {})}
        total += 0 if reason else b.est_s
        print(f"  {name:<14} {'SKIP: ' + reason if reason else f'~{b.est_s}s {params}'}")
    print(f"~{total // 60} min (plus cooldown waits)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
