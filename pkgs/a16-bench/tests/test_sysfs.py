"""Probe/Channels/summarize against a synthetic A16-like sysfs tree."""

import os
from pathlib import Path

import pytest

from a16_bench import meta
from a16_bench.sampler import Channels, Sampler, summarize
from a16_bench.sysfs import Probe


def w(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text + "\n")


@pytest.fixture
def tree(tmp_path):
    r = tmp_path
    zones = [("cpu-0-0-0-thermal", 41000), ("gpuss-0-thermal", 39000), ("pmh0104-thermal", 93000), ("aoss-0-thermal", 38000), ("cpu-0-0-0-thermal", 43000)]
    for i, (t, v) in enumerate(zones):
        w(r, f"sys/class/thermal/thermal_zone{i}/type", t)
        w(r, f"sys/class/thermal/thermal_zone{i}/temp", str(v))
    for pol, cpus, mx in (("policy0", "0-5", 3600000), ("policy6", "6-11", 4450000), ("policy12", "12-17", 4450000)):
        d = f"sys/devices/system/cpu/cpufreq/{pol}"
        w(r, f"{d}/related_cpus", cpus)
        w(r, f"{d}/cpuinfo_max_freq", str(mx))
        w(r, f"{d}/scaling_max_freq", str(mx))
        w(r, f"{d}/scaling_cur_freq", "1000000")
        w(r, f"{d}/scaling_governor", "schedutil")
    g = "sys/class/devfreq/3d00000.gpu"
    w(r, f"{g}/cur_freq", "310000000")
    w(r, f"{g}/max_freq", "1850000000")
    w(r, f"{g}/available_frequencies", "310000000 1185000000 1850000000")
    w(r, "sys/class/hwmon/hwmon3/name", "asus_glymur_ec")
    w(r, "sys/class/hwmon/hwmon3/fan1_input", "2400")
    b = "sys/class/power_supply/qcom-battmgr-bat"
    w(r, f"{b}/type", "Battery")
    w(r, f"{b}/status", "Discharging")
    w(r, f"{b}/power_now", "-6500000")
    w(r, f"{b}/capacity", "80")
    w(r, f"{b}/uevent", "POWER_SUPPLY_NAME=qcom-battmgr-bat\nPOWER_SUPPLY_TYPE=Battery\nPOWER_SUPPLY_STATUS=Discharging\nPOWER_SUPPLY_CAPACITY=80")
    a = "sys/class/power_supply/qcom-battmgr-ac"
    w(r, f"{a}/uevent", "POWER_SUPPLY_NAME=qcom-battmgr-ac\nPOWER_SUPPLY_TYPE=Mains\nPOWER_SUPPLY_ONLINE=0")
    for dev, st in (("a600000.usb", "enabled"), ("a800000.usb", "disabled")):
        w(r, f"sys/bus/platform/devices/{dev}/power/wakeup", st)
    # render nodes: the KMS card's and the GPU's
    (r / "devices/platform/ae01000.display-controller").mkdir(parents=True)
    (r / "devices/platform/3d00000.gpu").mkdir(parents=True)
    for n, dev in (("renderD128", "ae01000.display-controller"), ("renderD129", "3d00000.gpu")):
        (r / f"sys/class/drm/{n}").mkdir(parents=True)
        os.symlink(r / f"devices/platform/{dev}", r / f"sys/class/drm/{n}/device")
    w(r, "sys/kernel/debug/qcom_stats/cxsd", "Count: 3\nLast Entered At: 1\nLast Exited At: 2\nAccumulated Duration: 384000000")
    return r


def test_zones_and_temps(tree):
    p = Probe(tree)
    kinds = {z.type: z.kind for z in p.zones()}
    assert kinds == {"cpu-0-0-0-thermal": "cpu", "gpuss-0-thermal": "gpu", "pmh0104-thermal": "other", "aoss-0-thermal": "soc"}
    t = p.temps()
    assert t["cpu-0-0-0-thermal"] == 41.0 and t["cpu-0-0-0-thermal#thermal_zone4"] == 43.0
    # the PMIC temp alarm (stuck at 93) does not count as die temperature
    assert p.die_temp_max() == 43.0


def test_power_source_battery_then_ac(tree):
    p = Probe(tree)
    ps = p.power_source()
    assert ps["source"] == "battery" and ps["ac_online"] is False
    assert ps["battery"]["capacity"] == "80"
    w(tree, "sys/class/power_supply/qcom-battmgr-ac/uevent", "POWER_SUPPLY_TYPE=Mains\nPOWER_SUPPLY_ONLINE=1")
    w(tree, "sys/class/power_supply/qcom-battmgr-bat/uevent", "POWER_SUPPLY_TYPE=Battery\nPOWER_SUPPLY_STATUS=Charging")
    assert p.power_source()["source"] == "ac"


def test_misc_discovery(tree):
    p = Probe(tree)
    assert p.render_node() == "/dev/dri/renderD129"
    assert [c.cpus[0] for c in p.cpu_policies()] == [0, 6, 12]
    assert p.gpu_devfreq().name == "3d00000.gpu"
    assert p.wakealarm_rtc() is None
    w(tree, "sys/class/rtc/rtc0/wakealarm", "")
    assert p.wakealarm_rtc().name == "rtc0"
    assert {Path(k).name: v for k, v in p.usb_wakeup().items()} == {"a600000.usb": "enabled", "a800000.usb": "disabled"}
    assert "cxsd" in p.qcom_stats()


def test_channels_sample_and_summary(tree):
    ch = Channels(Probe(tree))
    assert ch.cpu_hw_max == {"policy0": 3600.0, "policy6": 4450.0, "policy12": 4450.0}
    assert ch.gpu_hw_max == 1850.0
    row = ch.sample(0)
    assert row["gpu.mhz"] == 310.0
    assert row["bat.power_mw"] == -6500.0 and row["bat.discharging"] == 1.0
    assert row["fan.asus_glymur_ec.fan1"] == 2400
    assert ch.die_max(row) == 43.0
    # throttle the GPU and policy6 in a second sample
    w(tree, "sys/class/devfreq/3d00000.gpu/max_freq", "1185000000")
    w(tree, "sys/class/devfreq/3d00000.gpu/cur_freq", "1185000000")
    w(tree, "sys/devices/system/cpu/cpufreq/policy6/scaling_max_freq", "2630000")
    w(tree, "sys/class/thermal/thermal_zone1/temp", "96000")
    row2 = ch.sample(1)
    s = summarize([row, row2], ch.zone_kind, ch.cpu_hw_max, ch.gpu_hw_max)
    assert s["temp_gpu_max_c"] == 96.0
    assert s["temp_die_max_c"] == 96.0 and s["temp_die_start_c"] == 43.0
    assert s["cpu_capped_s"] == 1 and s["gpu_capped_s"] == 1
    assert s["gpu_mhz_mean"] == pytest.approx((310 + 1185) / 2)
    assert s["bat_power_mw_mean"] == 6500.0
    assert s["fan_rpm_max"] == 2400


def test_sampler_thread_writes_csv_and_abort(tree, tmp_path):
    ch = Channels(Probe(tree))
    csv_path = tmp_path / "trace.csv"
    with Sampler(ch, csv_path, interval=0.05, abort_c=42.0) as s:
        assert s.over_limit.wait(2)
    lines = csv_path.read_text().splitlines()
    assert lines[0].startswith("t,tz.cpu-0-0-0-thermal,")
    assert len(lines) >= 2
    assert s.over_limit_c == 43.0


def test_meta_generation(tree):
    store = tree / "nix/store"
    for n in ("aaa-nixos-system-40", "bbb-nixos-system-41"):
        (store / n).mkdir(parents=True)
    prof = tree / "nix/var/nix/profiles"
    prof.mkdir(parents=True)
    os.symlink(store / "aaa-nixos-system-40", prof / "system-40-link")
    os.symlink(store / "bbb-nixos-system-41", prof / "system-41-link")
    os.symlink("system-41-link", prof / "system")
    (tree / "run").mkdir()
    os.symlink(store / "bbb-nixos-system-41", tree / "run/current-system")
    os.symlink(store / "aaa-nixos-system-40", tree / "run/booted-system")
    g = meta.generation(Probe(tree))
    assert g["booted_generation"] == 40 and g["current_generation"] == 41
    assert g["default_generation"] == 41 and g["switched_since_boot"] is True


def test_desktop_in_use():
    sess = [
        {"id": "c1", "Name": "sddm", "Type": "wayland", "Class": "greeter"},
        {"id": "2", "Name": "root", "Type": "tty", "Class": "user"},
        {"id": "3", "Name": 'user', "Type": "wayland", "Class": "user"},
    ]
    assert [s["id"] for s in meta.desktop_in_use(sess)] == ["3"]
