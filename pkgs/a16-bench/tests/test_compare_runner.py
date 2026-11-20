import argparse
import json

import pytest

from a16_bench import benchmarks, cli, compare, runner
from a16_bench.benchmarks import Bench, Skip


def run_doc(run_id, kernel, gen, metrics, status="ok", tools=None, cmdline="root=LABEL=a16 quiet", power="ac"):
    return {
        "schema": 1,
        "run_id": run_id,
        "profile": "quick",
        "complete": True,
        "meta": {
            "kernel": {"release": kernel},
            "system": {"booted_generation": gen},
            "power": {"source": power},
            "cmdline": cmdline,
            "tools": tools or {"vkmark": "2025.01"},
            "bench_version": "0.1.0",
        },
        "tests": {
            "gpu-vkmark": {"status": status, "metrics": metrics},
            "boot": {"status": "skipped", "reason": "boot not finished"},
        },
    }


def m(v, better="higher", headline=True):
    d = {"value": v, "unit": "x", "better": better}
    if headline:
        d["headline"] = True
    return d


def test_compare_verdicts_and_setup():
    a = run_doc("A", "7.3.0-rc5", 40, {"score": m(1000), "lat": m(10.0, "lower"), "info": m(5, None), "minor": m(1, headline=False)})
    b = run_doc(
        "B", "7.4.0-rc1", 41, {"score": m(900), "lat": m(9.0, "lower"), "info": m(6, None), "minor": m(2, headline=False), "new": m(3)},
        tools={"vkmark": "2025.02"}, cmdline="root=LABEL=a16 quiet clk_ignore_unused",
    )
    c = compare.compare(a, b, threshold=5)
    v = {r["metric"]: r["verdict"] for r in c["metrics"]}
    assert v == {"score": "worse", "lat": "better", "info": "info", "new": "missing"}
    assert c["worse"] == 1 and c["better"] == 1
    setup = {d["what"]: d for d in c["setup"]}
    assert setup["kernel"]["b"] == "7.4.0-rc1"
    assert setup["cmdline"] == {"what": "cmdline", "a": [], "b": ["clk_ignore_unused"]}
    assert "warn" in setup["tool vkmark"]
    assert c["status"][0]["test"] == "boot"
    # --all includes non-headline metrics
    assert "minor" in {r["metric"] for r in compare.compare(a, b, everything=True)["metrics"]}
    text = compare.format_text(c)
    assert "WORSE" in text and "kernel" in text


def test_compare_threshold():
    a = run_doc("A", "k", 1, {"score": m(1000)})
    b = run_doc("B", "k", 1, {"score": m(1030)})
    assert compare.compare(a, b, threshold=5)["metrics"][0]["verdict"] == "same"
    assert compare.compare(a, b, threshold=2)["metrics"][0]["verdict"] == "better"


def test_load_by_prefix_suffix(tmp_path):
    for rid in ("20261025T100000Z-quick-gen40", "20261026T100000Z-quick-gen41"):
        (tmp_path / rid).mkdir()
        (tmp_path / rid / "results.json").write_text(json.dumps({"run_id": rid}))
    assert compare.load("quick-gen41", tmp_path)["run_id"].endswith("gen41")
    assert compare.load("20261025", tmp_path)["run_id"].endswith("gen40")
    with pytest.raises(SystemExit):
        compare.load("quick", tmp_path)
    with pytest.raises(SystemExit):
        compare.load("nope", tmp_path)


def test_plan():
    assert [t for t, _ in runner.plan("quick")][0] == "boot"
    assert runner.plan("quick", skip=["boot"])[0][0] == "idle-power"
    assert runner.plan("full", only=["idle-power"]) == [("idle-power", {"seconds": 300})]
    assert runner.plan("quick", only=["s2idle"]) == [("s2idle", {})]
    with pytest.raises(SystemExit):
        runner.plan("nope")
    with pytest.raises(SystemExit):
        runner.plan("quick", only=["nope"])
    # every profile entry is a registered test, all-core loads only where opt-in makes sense
    for name, p in runner.PROFILES.items():
        for t, _ in p["tests"]:
            assert t in benchmarks.REGISTRY
        if name in ("quick", "gpu", "power"):
            assert not any(benchmarks.REGISTRY[t].allcore for t, _ in p["tests"])


def test_precheck():
    from a16_bench.sysfs import Probe

    b = Bench("x", "x", 1, ["definitely-not-a-tool-a16"], lambda *a: None)
    assert "missing tools" in b.precheck({}, Probe("/nonexistent"))
    b = Bench("x", "x", 1, [], lambda *a: None, allcore=True)
    assert "opt-in" in b.precheck({}, Probe("/nonexistent"))
    assert b.precheck({"allow_allcore": True}, Probe("/nonexistent")) is None
    b = Bench("x", "x", 1, [], lambda *a: None, power="battery")
    assert "needs battery" in b.precheck({}, Probe("/nonexistent"))


class NoInhibit:
    def resume(self):
        return True

    def pause(self):
        pass


def test_run_profile_end_to_end(tmp_path, monkeypatch):
    """A profile with fake tests: results.json, per-test statuses, index line."""

    def ok(ctx, r, p):
        out = ctx.run("echo", ["echo", "hello"], 10, r)
        assert out.strip() == "hello"
        r.metric("value", p["v"], "x", headline=True)

    def skip(ctx, r, p):
        raise Skip("not today")

    def boom(ctx, r, p):
        raise RuntimeError("kaputt")

    reg = {
        "t-ok": Bench("t-ok", "ok", 1, ["echo"], ok, params={"v": 1}, cooldown=False, sample_interval=0.05),
        "t-skip": Bench("t-skip", "skip", 1, [], skip, cooldown=False, sample=False),
        "t-fail": Bench("t-fail", "fail", 1, [], boom, cooldown=False, sample=False),
    }
    monkeypatch.setattr(runner, "REGISTRY", reg)
    monkeypatch.setattr(benchmarks, "REGISTRY", reg)
    monkeypatch.setitem(runner.PROFILES, "fake", {"about": "", "tests": [("t-ok", {"v": 2}), ("t-skip", {}), ("t-fail", {})]})
    monkeypatch.setattr(runner, "Inhibitor", NoInhibit)
    monkeypatch.setattr(runner.metamod, "sessions", lambda: [])
    args = argparse.Namespace(
        out=str(tmp_path), profile="fake", only=None, skip=None, param={"t-ok": {"v": 3}}, label="lbl",
        allow_allcore=False, allow_session=False, iperf_server=None, s2idle_manual=False, scratch_dir=None,
        cooldown_c=50.0, cooldown_timeout=1.0, no_cooldown=True, abort_c=105.0, argv=["run", "fake"],
    )
    run_dir = runner.run_profile(args)
    doc = json.loads((run_dir / "results.json").read_text())
    assert doc["complete"] is True and doc["label"] == "lbl"
    t = doc["tests"]
    assert t["t-ok"]["status"] == "ok" and t["t-ok"]["metrics"]["value"]["value"] == 3
    assert t["t-ok"]["trace"]["file"] == "traces/t-ok.csv"
    assert (run_dir / "raw" / "echo.txt").read_text().startswith("# echo hello")
    assert t["t-skip"] == {"status": "skipped", "reason": "not today", "conditions": t["t-skip"]["conditions"], "duration_s": t["t-skip"]["duration_s"]}
    assert t["t-fail"]["status"] == "failed" and "kaputt" in t["t-fail"]["reason"]
    assert (run_dir / "raw" / "t-fail-traceback.txt").exists()
    idx = [json.loads(l) for l in (tmp_path / "index.jsonl").read_text().splitlines()]
    assert idx[-1]["run_id"] == run_dir.name
    assert idx[-1]["headline"] == {"t-ok.value": 3}
    assert idx[-1]["status"] == {"t-ok": "ok", "t-skip": "skipped", "t-fail": "failed"}
    # the CLI reads it back
    assert cli.main(["--out", str(tmp_path), "show", "lbl"]) == 0
    assert cli.main(["--out", str(tmp_path), "compare", "lbl", str(run_dir)]) == 0


def test_ctx_run_timeout(tmp_path):
    from a16_bench.sysfs import Probe

    ctx = benchmarks.Ctx(tmp_path, Probe("/nonexistent"), {}, log=lambda s: None)
    with pytest.raises(benchmarks.Aborted):
        ctx.run("sleep", ["sleep", "5"], 0.5)
    with pytest.raises(RuntimeError):
        ctx.run("false", ["false"], 5)
    assert ctx.run("false2", ["false"], 5, check=False) == ""


def test_cli_lists(capsys):
    assert cli.main(["tests"]) == 0
    assert "gpu-vkmark" in capsys.readouterr().out
    assert cli.main(["profiles"]) == 0
    assert "quick" in capsys.readouterr().out
