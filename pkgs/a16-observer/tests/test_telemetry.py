import json
import os
import socket

import pytest

from a16_observer import telemetry as t

BOOT = "3f2c8a0e-1d2b-4c5d-9e8f-0a1b2c3d4e5f"
QCOM = "Count: 95\nLast Entered At: 19200000\nLast Exited At: 38400000\nAccumulated Duration: 192000000\n"
DDR = (
    "DDR LPM Stat Name:0xd4\tcount:3\tDuration (ticks):19200000\n"
    "DDR Freq 200Mhz:\tCP IDX:1\tcount:1\tDuration (ticks):0\n"
    "DDR Freq 547Mhz:\tCP IDX:2\tcount:4\tDuration (ticks):38400000\n"
)


def w(root, path, content, mode="w"):
    p = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode) as f:
        f.write(content)
    return p


def regmap_text(n=0x2000, holes=()):
    return "".join(f"{r:04x}: {r & 0xFF:02x}\n" for r in range(n) if r not in holes)


@pytest.fixture
def tree(tmp_path):
    r = str(tmp_path)
    w(r, "/proc/sys/kernel/random/boot_id", BOOT + "\n")
    w(r, "/proc/sys/kernel/osrelease", "7.3.0-a16\n")
    w(r, "/proc/uptime", "1234.56 9999.0\n")
    w(r, "/proc/loadavg", "0.42 0.30 0.20 1/300 4242\n")
    w(r, "/proc/stat", "cpu 1 2 3\nbtime 1792800000\n")
    for i, (typ, temp) in enumerate(
        [("qcom-battmgr-bat", 30400), ("cpu-0-0-0-thermal", 45000), ("cpu-0-0-1-thermal", 99000), ("gpuss-0-thermal", 50000)]
    ):
        w(r, f"/sys/class/thermal/thermal_zone{i}/type", typ + "\n")
        w(r, f"/sys/class/thermal/thermal_zone{i}/temp", f"{temp}\n")
    bat = "/sys/class/power_supply/qcom-battmgr-bat"
    for k, v in dict(type="Battery", status="Discharging", power_now="6510000", capacity="81", energy_now="50000000", energy_full="70000000").items():
        w(r, f"{bat}/{k}", v + "\n")
    w(r, "/sys/class/power_supply/qcom-battmgr-ac/type", "Mains\n")
    w(r, "/sys/class/power_supply/qcom-battmgr-ac/online", "0\n")
    w(r, "/sys/class/power_supply/ucsi-source-psy-pmic_glink.ucsi.02/type", "USB\n")
    w(r, "/sys/class/power_supply/ucsi-source-psy-pmic_glink.ucsi.02/online", "1\n")
    w(r, "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq", "3200000\n")
    w(r, "/sys/devices/system/cpu/cpufreq/policy6/scaling_cur_freq", "1200000\n")
    w(r, "/sys/power/suspend_stats/success", "17\n")
    w(r, "/sys/power/suspend_stats/fail", "1\n")
    w(r, "/sys/power/suspend_stats/last_failed_dev", "a600000.usb\n")
    w(r, "/sys/power/suspend_stats/last_failed_errno", "-16\n")
    w(r, "/sys/power/suspend_stats/last_failed_step", "suspend\n")
    w(r, "/sys/kernel/debug/qcom_stats/aosd", QCOM)
    w(r, "/sys/kernel/debug/qcom_stats/ddr_stats", DDR)
    w(r, "/sys/class/wakeup/wakeup0/name", "alarmtimer\n")
    w(r, "/sys/class/wakeup/wakeup0/event_count", "5\n")
    w(r, "/sys/class/wakeup/wakeup0/wakeup_count", "1\n")
    w(r, "/sys/class/wakeup/wakeup0/total_time_ms", "1500\n")
    w(r, "/sys/class/remoteproc/remoteproc0/name", "adsp\n")
    w(r, "/sys/class/remoteproc/remoteproc0/state", "running\n")
    os.makedirs(os.path.join(r, "nix/store/aaa-nixos-system"))
    os.makedirs(os.path.join(r, "nix/store/bbb-nixos-system"))
    os.makedirs(os.path.join(r, "nix/var/nix/profiles"))
    os.makedirs(os.path.join(r, "run"))
    os.symlink(os.path.join(r, "nix/store/aaa-nixos-system"), os.path.join(r, "run/booted-system"))
    os.symlink(os.path.join(r, "nix/store/aaa-nixos-system"), os.path.join(r, "nix/var/nix/profiles/system-31-link"))
    os.symlink(os.path.join(r, "nix/store/bbb-nixos-system"), os.path.join(r, "nix/var/nix/profiles/system-32-link"))
    entry = "nixos-generation-31+2-1.conf\0".encode("utf-16-le")
    w(r, "/sys/firmware/efi/efivars/LoaderEntrySelected-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f", b"\x06\x00\x00\x00" + entry, "wb")
    os.makedirs(os.path.join(r, "boot/loader/entries"))
    os.makedirs(os.path.join(r, "var/lib/systemd/pstore/1792800001"))
    # PMK8850 on SPMI bus 0, SID 0, with its PON node.
    node = "proc/device-tree/soc@0/spmi@c426000/pmic@0"
    w(r, f"/{node}/pon@1300/compatible", "qcom,pmk8350-pon\0")
    w(r, f"/{node}/pon@1300/reg", bytes.fromhex("0000130000000800"), "wb")
    w(r, f"/{node}/pon@1300/reg-names", b"hlos\0pbs\0", "wb")
    os.makedirs(os.path.join(r, "sys/bus/spmi/devices/0-00"))
    os.symlink(os.path.join(r, node), os.path.join(r, "sys/bus/spmi/devices/0-00/of_node"))
    os.makedirs(os.path.join(r, "sys/bus/spmi/devices/0-01"))  # no of_node
    w(r, "/sys/kernel/debug/regmap/0-00/registers", regmap_text())
    return r


def test_payload(tree):
    src = t.HeartbeatSource(tree, state_file=os.path.join(tree, "state"))
    line = src.payload(now=1000.0)
    boot, up, *kv = line.split()
    f = dict(x.split("=", 1) for x in kv)
    assert boot == BOOT and up == "1234.56"
    assert f["v"] == "2" and f["iv"] == "1"
    assert f["temp_max_mc"] == "50000"  # cpu-0-0-1 is outside the default list
    assert f["temp_bat_mc"] == "30400"
    assert f["bat_uw"] == "-6510000" and f["bat_pct"] == "81" and f["ac"] == "1"
    assert f["cpu_mhz_max"] == "3200" and f["load1"] == "0.42" and f["susp"] == "17"
    assert f["state"] == "run"
    assert line.endswith("\n")


def test_state_file(tree):
    sf = os.path.join(tree, "state")
    with open(sf, "w") as f:
        f.write("suspending 100.0\n")
    assert t.read_state(sf, 5000.0) == "suspending"
    with open(sf, "w") as f:
        f.write("resumed 100.0\n")
    assert t.read_state(sf, 105.0) == "resumed"
    assert t.read_state(sf, 200.0) == "run"
    assert t.read_state(os.path.join(tree, "missing"), 1.0) == "run"


def test_mark_sleep(tree):
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    port = rx.getsockname()[1]
    sf = os.path.join(tree, "run/a16-heartbeat/state")
    log = os.path.join(tree, "var/lib/a16/sleep.log")
    t.main(["--root", tree, "mark-sleep", "pre", "--target", f"127.0.0.1:{port}", "--state-file", sf, "--log", log])
    d = rx.recv(1000).decode()
    assert d.startswith(BOOT) and "state=suspending" in d and "susp=17" in d
    assert open(sf).read().startswith("suspending ")
    assert " pre " in open(log).read()


def test_metrics(tree):
    text = t.collect(tree, now=1792800100.0)
    lines = text.splitlines()
    assert 'a16_qcom_stats_count{subsystem="aosd"} 95' in lines
    assert 'a16_qcom_stats_seconds{subsystem="aosd"} 10' in lines
    assert 'a16_ddr_lpm_seconds{stat="0xd4"} 1' in lines
    assert 'a16_ddr_freq_count{mhz="547"} 4' in lines
    assert 'a16_suspend_stats{stat="success"} 17' in lines
    assert 'a16_suspend_last_failure_info{dev="a600000.usb",errno="-16",step="suspend"} 1' in lines
    assert 'a16_wakeup_active_seconds{source="alarmtimer"} 1.5' in lines
    assert "a16_battery_power_watts -6.51" in lines
    assert "a16_battery_energy_wh 50" in lines
    assert "a16_ac_online 1" in lines
    assert (
        f'a16_boot_info{{boot_id="{BOOT}",generation="31",kernel="7.3.0-a16",entry="nixos-generation-31+2-1.conf"}} 1'
        in lines
    )
    assert "a16_boot_count_tries_left 2" in lines
    assert "a16_boot_blessed 0" in lines
    assert "a16_pstore_records 1" in lines
    assert 'a16_remoteproc_running{name="adsp",state="running"} 1' in lines
    assert 'a16_cpufreq_hz{policy="policy0"} 3200000000' in lines
    assert "a16_pm_wakeup_irq" not in text
    assert "a16_textfile_errors" not in text
    assert "# TYPE a16_qcom_stats_count counter" in lines


def test_metrics_out(tree, tmp_path):
    out = tmp_path / "m" / "a16.prom"
    t.main(["--root", tree, "metrics", "--out", str(out)])
    assert "a16_boot_info" in out.read_text()
    assert oct(out.stat().st_mode & 0o777) == "0o644"


def test_blessed(tree):
    open(os.path.join(tree, "boot/loader/entries/nixos-generation-31.conf"), "w").close()
    assert t.boot_identity(tree)["blessed"] == 1


def test_find_pon(tree):
    assert t.find_pon(tree) == [
        {"regmap": "0-00", "name": "hlos", "base": 0x1300, "compatible": "qcom,pmk8350-pon"},
        {"regmap": "0-00", "name": "pbs", "base": 0x800, "compatible": "qcom,pmk8350-pon"},
    ]


def test_read_regmap_seek(tree):
    path = os.path.join(tree, "sys/kernel/debug/regmap/0-00/registers")
    data = t.read_regmap(path, 0x1300)
    assert data == bytes(range(256))


def test_read_regmap_fallback(tmp_path):
    p = tmp_path / "registers"
    p.write_text(regmap_text(0x1400, holes={0x10, 0x11}))
    data = t.read_regmap(str(p), 0x1300, 16)
    assert data == bytes(range(16))


def test_boot_record(tree):
    rec = t.boot_record(tree, journal_end=False)
    assert rec["generation"] == "31"
    hlos = [p for p in rec["pon"] if p["name"] == "hlos"][0]
    assert hlos["hex"] == bytes(range(256)).hex()
    pbs = [p for p in rec["pon"] if p["name"] == "pbs"][0]
    assert pbs["hex"][:4] == "0001"


def test_classify_end():
    assert t.classify_end("") == "unknown"
    assert t.classify_end("Reached target System Shutdown.\nFinished System Reboot.\nReached target System Reboot.\n") == "reboot"
    assert t.classify_end("Reached target System Power Off.\nsystemd-poweroff.service: Deactivated\n") == "poweroff"
    assert t.classify_end("Sending SIGTERM to remaining processes...\nReceived SIGTERM from PID 1 (systemd-shutdow).\n") == "shutdown"
    assert t.classify_end("kernel: usb 1-1: new device\n") == "unclean"


def test_replay_payloads():
    recs = [b"6,1,100,-;first\n", b"4,2,200,-;second\n SUBSYSTEM=usb\n", b"6,3,300,c;no newline"]
    out = list(t.replay_payloads(recs, "7.3.0", BOOT))
    assert out[0] == f"7.3.0,6,1,100,-;first\n boot={BOOT}\n replay=1\n".encode()
    assert out[1].endswith(f" SUBSYSTEM=usb\n boot={BOOT}\n replay=1\n".encode())
    assert out[2] == f"7.3.0,6,3,300,c;no newline\n boot={BOOT}\n replay=1\n".encode()
    # The sink reads them back as records of that boot, release and all.
    from a16_observer.sinks import parse_netconsole

    r = parse_netconsole(out[1])[0]
    assert r["seq"] == 2 and r["release"] == "7.3.0" and r["meta"] == {"SUBSYSTEM": "usb", "boot": BOOT, "replay": "1"}
    assert list(t.replay_payloads([b"6,1,1,-;x\n"], None, BOOT))[0].startswith(b"6,1,1,-;x\n")


def test_nc_configure_and_match(tmp_path):
    tdir = str(tmp_path / "a16")
    os.makedirs(os.path.join(tdir, "userdata"))
    for f in ("enabled", "extended", "release", "dev_name", "local_ip", "remote_ip", "remote_mac", "remote_port", "local_port"):
        w(tdir, f, "0\n")
    route = {"dev": "enu1", "src": "192.0.2.5", "nexthop": "192.0.2.20"}
    want = t.nc_want(route, "AA:BB:CC:DD:EE:FF", "192.0.2.20", 6667, 6665)
    assert not t.nc_matches(tdir, want, BOOT)
    t.nc_configure(tdir, want, BOOT, release=True)
    assert t.read(os.path.join(tdir, "enabled")) == "1" and t.read(os.path.join(tdir, "extended")) == "1"
    assert t.read(os.path.join(tdir, "release")) == "1"
    assert t.read(os.path.join(tdir, "remote_mac")) == "aa:bb:cc:dd:ee:ff"
    assert t.read(os.path.join(tdir, "userdata", "boot", "value")) == BOOT
    assert t.nc_matches(tdir, want, BOOT)
    assert not t.nc_matches(tdir, {**want, "dev_name": "wlP4p1s0"}, BOOT)
    assert not t.nc_matches(tdir, want, "other-boot")
    assert t.nc_disable(tdir) and not t.nc_disable(tdir)


def test_kmsg_records(tmp_path):
    # A regular file stands in for /dev/kmsg (which returns one record per
    # read; a file returns everything at once, which is fine for this check).
    p = w(str(tmp_path), "kmsg", "6,1,1,-;a\n")
    assert list(t.kmsg_records(p)) == [b"6,1,1,-;a\n"]


def test_pstore_ship(tmp_path, capsys):
    r = str(tmp_path)
    rec = "1792815680"
    w(r, f"/var/lib/systemd/pstore/{rec}/001/dmesg.txt", "Oops#1 Part1\n<4>[ 1.0] ok\n<0>[ 2.0] Kernel panic - not syncing: boom\n")
    w(r, f"/var/lib/systemd/pstore/{rec}/001/dmesg-efi_pstore-179281568010001", "raw part\n")
    w(r, "/var/lib/systemd/pstore/1792815690/dmesg-efi_pstore-1", "Unable to handle kernel NULL pointer\n")
    state = os.path.join(r, "state")
    args = t.argparse.Namespace(root=r, dir="/var/lib/systemd/pstore", state=state)
    assert t.cmd_pstore_ship(args) == 0
    out = capsys.readouterr().out
    assert "a16-pstore[4]: pstore record 1792815680 from 2026-" in out
    # The summary names the panic, not the "Oops#1 Part1" header before it.
    assert "bytes; <0>[ 2.0] Kernel panic - not syncing: boom" in out and "A16_PSTORE_ID=1792815680" in out
    assert "MESSAGE_ID=" + t.PSTORE_MESSAGE_ID in out
    assert "A16_PSTORE_FILES=001/dmesg.txt " in out  # the reassembled file, not the raw part
    assert "Unable to handle kernel NULL pointer" in out
    assert open(state).read().split() == ["1792815680", "1792815690"]
    assert t.cmd_pstore_ship(args) == 0
    assert "shipped 0 pstore record(s)" in capsys.readouterr().out


def test_shorten():
    text = "\n".join(f"line {i}" for i in range(10000))
    s = t.shorten(text, limit=2000, lines=5)
    assert s.startswith("line 0\n") and "lines cut" in s and len(s.encode()) <= 2000
    assert t.shorten("short") == "short"


def test_redflags_counter(tree, tmp_path):
    """a16-telemetry redflags on a recorded kernel log: counts per pattern,
    every pattern exported (zeros too), and a restart continues from the state
    file instead of counting the ring buffer again."""
    kmsg = os.path.join(tree, "dev", "kmsg")
    os.makedirs(os.path.dirname(kmsg), exist_ok=True)
    with open(kmsg, "w") as f:
        f.write("6,100,1000,-;usb 5-1: new SuperSpeed USB device number 2 using xhci-hcd\n SUBSYSTEM=usb\n")
        f.write("3,101,8045181370,-;qcom-soundwire 6c80000.soundwire: qcom_swrm_irq_handler: SWR bus clash detected\n")
        f.write("3,102,8045181618,-;wsa884x-codec sdw:1:0:0217:0204:00:1: Parity error detected\n")
        f.write("3,104,8045181700,-;wsa884x-codec sdw:1:0:0217:0204:00:0: Parity error detected\n")
        f.write("3,105,8045190000,-;dwc3-qcom a600000.usb: port-1 HS-PHY not in L2\n")
    out, state = str(tmp_path / "m" / "redflags.prom"), str(tmp_path / "s" / "state.json")
    args = ["--root", tree, "redflags", "--out", out, "--state", state, "--kmsg", "/dev/kmsg"]
    t.main(args)
    lines = open(out).read().splitlines()
    assert 'a16_redflag_total{id="swr-bus-clash",severity="crit",subsystem="audio"} 1' in lines
    assert 'a16_redflag_total{id="wsa-parity",severity="crit",subsystem="audio"} 2' in lines
    assert 'a16_redflag_total{id="usb-hsphy-not-l2",severity="info",subsystem="usb"} 1' in lines
    assert 'a16_redflag_total{id="oops",severity="crit",subsystem="kernel"} 0' in lines
    assert "a16_redflag_kmsg_seq 105" in lines and "a16_redflag_kmsg_lost_total 1" in lines
    assert "# TYPE a16_redflag_total counter" in lines
    # restart: same boot, two new records; old ones are not counted again
    with open(kmsg, "a") as f:
        f.write("3,106,8046000000,-;qcom-soundwire 6c80000.soundwire: qcom_swrm_irq_handler: SWR bus clash detected\n")
    t.main(args)
    assert 'a16_redflag_total{id="swr-bus-clash",severity="crit",subsystem="audio"} 2' in open(out).read().splitlines()
    assert 'a16_redflag_total{id="wsa-parity",severity="crit",subsystem="audio"} 2' in open(out).read().splitlines()
    # a new boot starts from zero
    with open(os.path.join(tree, "proc/sys/kernel/random/boot_id"), "w") as f:
        f.write("another-boot\n")
    t.main(args)
    assert 'a16_redflag_total{id="swr-bus-clash",severity="crit",subsystem="audio"} 2' in open(out).read().splitlines()
    assert json.load(open(state))["boot_id"] == "another-boot"
