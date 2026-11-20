import json
from pathlib import Path

import pytest

from a16_bench import parsers

FX = Path(__file__).parent / "fixtures"


def fx(name):
    return (FX / name).read_text()


def test_glmark2():
    r = parsers.parse_mark(fx("glmark2-gbm.txt"))
    assert r["score"] == 28578
    assert r["info"]["GL_RENDERER"].startswith("AMD Radeon")
    assert [s["scene"] for s in r["scenes"]] == ["build", "texture", "shading", "terrain"]
    assert r["scenes"][3]["fps"] == 2155
    assert r["scenes"][3]["frametime_ms"] == pytest.approx(0.464)
    assert parsers.scene_key(r["scenes"][2]) == "shading[duration=1:shading=phong]"
    assert not parsers.is_software_renderer(r["info"])


def test_vkmark():
    r = parsers.parse_mark(fx("vkmark-headless.txt"))
    assert r["score"] == 68775
    assert r["info"]["Device Name"].startswith("AMD Radeon")
    assert len(r["scenes"]) == 3
    assert r["scenes"][0] == {"scene": "cube", "options": "duration=1", "fps": 65494, "frametime_ms": 0.015}


def test_mark_unsupported_and_default_and_llvmpipe():
    text = (
        "    GL_RENDERER:    llvmpipe (LLVM 19, 256 bits)\n"
        "[desktop] <default>: Unsupported\n"
        "[clear] <default>: FPS: 100 FrameTime: 10.000 ms\n"
        "                                  glmark2 Score: 100 \n"
    )
    r = parsers.parse_mark(text)
    assert r["failed"] == [{"scene": "desktop", "options": "<default>", "error": "Unsupported"}]
    assert parsers.scene_key(r["scenes"][0]) == "clear"
    assert parsers.is_software_renderer(r["info"])


def test_7zip():
    r = parsers.parse_7zip_bench(fx("7zz-mt1.txt"))
    assert r["total_rating_mips"] == 9272
    assert r["total_usage_pct"] == 100
    assert r["compress_rating_mips"] == 7463
    assert r["decompress_rating_mips"] == 11080
    with pytest.raises(ValueError):
        parsers.parse_7zip_bench("nothing here")


def test_stressng():
    r = parsers.parse_stressng_yaml(fx("stressng-cpu.yaml"))
    assert r["cpu"]["bogo_ops"] == 7398
    assert r["cpu"]["bogo_ops_s_real"] == pytest.approx(2465.600265)


def test_sysbench():
    r = parsers.parse_sysbench_memory(fx("sysbench-mem.txt"))
    assert r["mib_s"] == pytest.approx(198935.03)
    assert r["total_time_s"] == pytest.approx(3.0004)


def test_tinymembench():
    r = parsers.parse_tinymembench(fx("tinymembench.txt"))
    bw = r["bandwidth_mb_s"]
    assert bw["c_copy"] == pytest.approx(25144.0)
    assert bw["standard_memcpy"] == pytest.approx(35721.1)
    assert bw["movsd_copy_from_framebuffer"] == pytest.approx(59203.3)
    assert r["latency_ns"]["nohuge"][67108864] == {"single": 100.9, "dual": 135.5}
    assert r["latency_ns"]["huge"][1024] == {"single": 0.0, "dual": 0.0}
    assert len(r["latency_ns"]["huge"]) == 17


def test_fio():
    r = parsers.parse_fio(json.loads(fx("fio.json")))
    assert set(r) == {"seqwrite", "seqread", "randread-qd32", "randwrite-qd32", "randread-qd1"}
    assert r["seqread"]["read_mb_s"] == pytest.approx(6632471 * 1024 / 1e6)
    assert "write_mb_s" not in r["seqread"]
    assert r["randread-qd1"]["read_lat_us"] > 0
    assert r["randread-qd1"]["read_p99_us"] >= r["randread-qd1"]["read_lat_us"] * 0.5
    assert r["seqwrite"]["write_iops"] > 0


def test_iperf3():
    tx = parsers.parse_iperf3(json.loads(fx("iperf3.json")))
    rx = parsers.parse_iperf3(json.loads(fx("iperf3-R.json")))
    assert tx["mbit_s"] == pytest.approx(125174.914904, rel=1e-6)
    assert tx["retransmits"] == 7
    assert rx["mbit_s"] > 100000
    with pytest.raises(ValueError):
        parsers.parse_iperf3({"error": "unable to connect to server"})


def test_systemd_analyze():
    r = parsers.parse_systemd_analyze_time(fx("sa-time.txt"))
    assert r == {
        "firmware_s": 15.576,
        "loader_s": 9.051,
        "kernel_s": 1.675,
        "initrd_s": 4.267,
        "userspace_s": pytest.approx(122.451),
        "total_s": pytest.approx(153.023),
        "graphical_target_s": pytest.approx(122.451),
    }
    a16 = "Startup finished in 16s (firmware) + 3.800s (loader) + 600ms (kernel) + 6.3s (initrd) + 20.1s (userspace) = 46.8s\n"
    assert parsers.parse_systemd_analyze_time(a16)["kernel_s"] == pytest.approx(0.6)
    with pytest.raises(ValueError):
        parsers.parse_systemd_analyze_time("Bootup is not yet finished (org.freedesktop.systemd1.Manager.FinishTimestampMonotonic=0).")


@pytest.mark.parametrize(
    "s,v",
    [("2min 2.451s", 122.451), ("600ms", 0.6), ("1h 2min 3s", 3723), ("15.576s", 15.576), ("850us", 0.00085)],
)
def test_duration(s, v):
    assert parsers.parse_duration(s) == pytest.approx(v)


def test_qcom_stat():
    text = "Count: 12\nLast Entered At: 1234\nLast Exited At: 5678\nAccumulated Duration: 1920000000\n"
    assert parsers.parse_qcom_stat(text) == {"count": 12, "accumulated_ticks": 1920000000}
    assert parsers.parse_qcom_stat("Count: 0x10\nAccumulated Duration: 0\nClient Votes: 0x3\n") == {"count": 16, "accumulated_ticks": 0}


def test_uevent():
    ue = "POWER_SUPPLY_NAME=qcom-battmgr-bat\nPOWER_SUPPLY_TYPE=Battery\nPOWER_SUPPLY_STATUS=Discharging\nPOWER_SUPPLY_POWER_NOW=-6123000\n"
    assert parsers.parse_uevent(ue) == {"name": "qcom-battmgr-bat", "type": "Battery", "status": "Discharging", "power_now": "-6123000"}
