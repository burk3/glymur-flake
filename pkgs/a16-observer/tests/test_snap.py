"""a16-snap: parsers on recorded A16 output (tests/fixtures/snap/), the diff,
the store, and the usb collectors on a synthetic sysfs/debugfs tree (including
the runtime-PM gate on register-backed debugfs)."""

import json
import os

import pytest

from a16_observer.snap import collectors  # noqa: F401
from a16_observer.snap import diff as snapdiff
from a16_observer.snap import main as snapmain
from a16_observer.snap import parse, store
from a16_observer.snap.core import CmdResult, Ctx, run_collectors

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "snap")


def fx(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


# --- parsers -----------------------------------------------------------------------
def test_bracketed():
    assert parse.bracketed("[host] device") == "host"
    assert parse.bracketed("source [sink]") == "sink"
    assert parse.bracketed("3.0A") == "3.0A"
    assert parse.bracketed(None) is None


def test_lsusb_tree():
    t = parse.lsusb_tree(fx("lsusb-t.txt"))
    assert t["2"]["speed"] == "10000M" and t["2"]["ports"] == "2p"
    nic = t["2"]["children"][0]
    assert nic["driver"] == "r8152" and nic["speed"] == "5000M" and nic["port"] == 1
    assert t["3"]["children"][0]["driver"] == "ftdi_sio"
    assert t["6"]["children"] == []


def test_interrupts():
    irq = parse.interrupts(fx("interrupts.txt"))
    x = next(v for v in irq.values() if v.get("name") == "xhci-hcd:usb5")
    assert x["count"] > 0 and x["chip"] == "GICv3" and x["type"] == "Level"
    assert any(v.get("name") == "dp_hs_phy_1" for v in irq.values())


def test_regulator_summary_and_filter():
    regs = parse.regulator_summary(fx("regulator_summary.txt"))
    assert regs["vreg_l1c_e0"]["voltage_mv"] == 936
    assert "fde000.phy-vdda-phy" in regs["vreg_l1c_e0"]["consumers"]
    assert regs["vreg_l1c_e0"]["consumers"]["fde000.phy-vdda-phy"]["load_ma"] == 21
    sel = parse.regulators_for(regs, r"^(fde000\.phy)\b")
    assert "vreg_l1c_e0" in sel and "regulator-dummy" not in sel


def test_genpd_summary():
    g = parse.genpd_summary(fx("pm_genpd_summary.txt"))
    assert g["disp_cc_mdss_core_gdsc"]["status"] == "on"
    assert g["disp_cc_mdss_core_gdsc"]["devices"]["ae00000.display-subsystem"] == "active"
    assert g["gcc_usb30_sec_gdsc"]["devices"]["a800000.usb"] == "active"


def test_wakeup_and_qcom_stats():
    w = parse.wakeup_sources(fx("wakeup_sources.txt"))
    assert w["gpio-keys"]["event_count"] >= 1
    assert all(v["event_count"] or v["active_count"] or v["wakeup_count"] for v in w.values())
    s = parse.qcom_stats(fx("qcom_stats_cxsd.txt"))
    assert s["count"] == 4 and s["accumulated_duration"] == 1576818762
    d = parse.ddr_stats(fx("ddr_stats.txt"))
    assert d["lpm"]["0xd4"]["count"] == 4
    assert d["freq"]["4224Mhz"]["cp_idx"] == 10


def test_dp_debug():
    d = parse.dp_debug(fx("dp_debug.txt"))
    assert d["dp_link.num_lanes"] == 4 and d["dp_link.bw_code"] == 10
    assert d["dp_panel_info.active"] == "1920x1080"


def test_regdump_portsc():
    r = parse.regdump(fx("dwc3_regdump.txt"))
    assert r["GCTL"].startswith("0x")
    key = {k for k in r if parse.DWC3_KEY_REGS.match(k)}
    assert {"GCTL", "GSTS", "GDBGLTSSM", "GUSB3PIPECTL(0)"} <= key
    assert "GUSB2PHYCFG(5)" not in key
    p = parse.portsc("0x00001203 Speed=4 Link=U0 CCS PP PED\n")
    assert p == {"raw": "0x00001203", "speed": "4", "link": "U0", "flags": "CCS PP PED"}


def test_ethtool():
    e = parse.ethtool(fx("ethtool.txt"))
    assert e["Speed"] == "10000Mb/s" and e["Link detected"] == "yes"
    assert "5000baseT/Full" in e["Supported link modes"]
    s = parse.ethtool_stats(fx("ethtool-S.txt"))
    assert s["InErrors"] == 0 and s["InPackets"] > 0


def test_kmsg_and_klog_counts():
    recs = [parse.parse_kmsg_record(l) for l in fx("kmsg.txt").splitlines()]
    assert all(recs)
    assert parse.fmt_kmsg(recs[0]).startswith("[")
    c = parse.klog_counts([r["msg"] for r in recs], {"xhci-hcd.3.auto": [5, 6]})
    assert c["total"]["firmware_missing"] >= 2
    assert c["by_bus"]["6"]["firmware_missing"] >= 2
    assert c["total"]["err_71"] > 0
    # psci "[Firmware Bug]: failed ..." is not a missing firmware file
    assert parse.klog_counts(["psci: [Firmware Bug]: failed to set PC mode: -3"])["total"]["firmware_missing"] == 0
    assert parse.klog_bus("r8152-cfgselector 6-1: reset SuperSpeed Plus Gen 2x1 USB device number 2 using xhci-hcd") == [6]
    assert parse.klog_bus("usb usb5-port1: attempt power cycle") == [5]
    assert parse.klog_bus("r8152 6-1:1.0 enu1: carrier on") == [6]


def test_kmsg_continuation():
    r = parse.parse_kmsg_record("6,42,1500000,-;usb 5-1: new device\n SUBSYSTEM=usb\n DEVICE=c189:1")
    assert r["seq"] == 42 and r["msg"] == "usb 5-1: new device" and r["props"]["SUBSYSTEM"] == "usb"


# --- diff ---------------------------------------------------------------------------
def _snap(mode, irq, boot="b1", extra=None):
    data = {"c2": {"port": {"power_operation_mode": mode, "data_role": "host"}}}
    if extra:
        data["c2"].update(extra)
    return {
        "id": f"usb/x_{mode}",
        "label": mode,
        "taken_at": "t",
        "boot_id": boot,
        "generation": 65,
        "kernel": "7.3.0",
        "port": "c2",
        "topic": "usb",
        "collectors": {
            "usb.typec": {"ok": True, "data": data, "elapsed_ms": 3},
            "usb.irqs": {"ok": True, "data": {"199 xhci-hcd:usb5": irq}, "elapsed_ms": 1},
            "usb.tree": {"ok": True, "data": {"c2": {"usb5": {"power": {"active_duration": irq * 7}}}}, "elapsed_ms": 1},
        },
        "skipped": [],
        "errors": [],
    }


def test_diff_fields():
    a = _snap("3.0A", 100)
    b = _snap("default", 130, extra={"partner": {"supports_usb_power_delivery": "no"}})
    d = snapdiff.diff(a, b)
    assert d["changed"]["usb.typec.c2.port.power_operation_mode"] == ["3.0A", "default"]
    assert d["changed"]["usb.irqs.199 xhci-hcd:usb5"] == [100, 130]
    assert "usb.typec.c2.partner.supports_usb_power_delivery" in d["added"]
    assert not any("active_duration" in p for p in d["changed"])  # volatile
    assert any("active_duration" in p for p in snapdiff.diff(a, b, include_volatile=True)["changed"])
    txt = snapdiff.format_diff(d)
    assert "~ usb.irqs.199 xhci-hcd:usb5: 100 -> 130  (+30)" in txt
    assert "'3.0A' -> 'default'" in txt
    only = snapdiff.diff(a, b, grep="power_operation")
    assert list(only["changed"]) == ["usb.typec.c2.port.power_operation_mode"] and not only["added"]


def test_diff_failed_collector_and_boot_note():
    a = _snap("3.0A", 1)
    b = _snap("3.0A", 1, boot="b2")
    b["collectors"]["usb.irqs"] = {"ok": False, "error": "OSError: boom"}
    d = snapdiff.diff(a, b)
    assert d["added"]["usb.irqs.error"] == "OSError: boom"
    assert "different boots" in snapdiff.format_diff(d)


# --- store --------------------------------------------------------------------------
def test_store_roundtrip(tmp_path):
    s = str(tmp_path)
    ids = []
    for i, lab in enumerate(["boot present", "hotplug", "hotplug"]):
        snap = _snap("3.0A", i) | {"id": store.new_id("usb", lab, 1_800_000_000 + i * 60), "label": lab, "taken_at": f"2027-01-15T08:0{i}:00Z"}
        store.write(s, snap, {"typec": "x"}, "dmesg\n")
        ids.append(snap["id"])
    assert ids[0] == "usb/20270115T080000Z_boot-present"
    es = store.entries(s)
    assert [e["id"] for e in es] == ids
    assert store.resolve(s, "@-1").endswith(ids[2])
    assert store.resolve(s, "@-3").endswith(ids[0])
    assert store.resolve(s, "boot-present").endswith(ids[0])
    with pytest.raises(LookupError):
        store.resolve(s, "hotplug")  # two matches
    d = store.resolve(s, ids[1])
    assert os.path.isfile(os.path.join(d, "raw", "typec.txt")) and os.path.isfile(os.path.join(d, "dmesg.txt"))
    assert store.prune(s, 2) == [ids[0]]
    assert [e["id"] for e in store.entries(s)] == ids[1:]


def test_store_same_second(tmp_path):
    s = str(tmp_path)
    a = _snap("x", 1) | {"id": "usb/T_same"}
    b = _snap("x", 1) | {"id": "usb/T_same"}
    store.write(s, a, {}, "")
    store.write(s, b, {}, "")
    assert b["id"] == "usb/T_same-2"


# --- collectors on a synthetic tree ---------------------------------------------------
def w(root, path, text=""):
    p = root + path
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text + "\n")


def ln(root, path, target):
    p = root + path
    os.makedirs(os.path.dirname(p), exist_ok=True)
    os.symlink(target, p)


@pytest.fixture
def a16(tmp_path):
    """C2 only: a800000.usb with xhci-hcd.3.auto (usb5/usb6, an RTL8159 on 6-1),
    typec port1 with a PD partner, eUSB2 PHY + SMB2370 repeater, debugfs."""
    r = str(tmp_path)
    c = "/sys/devices/platform/soc@0/a800000.usb"
    x = f"{c}/xhci-hcd.3.auto"
    w(r, f"{c}/power/runtime_status", "active")
    w(r, f"{x}/power/runtime_status", "active")
    os.makedirs(r + f"{c}/supplier:platform:fdd000.phy")
    os.makedirs(r + f"{c}/supplier:phy:phy-fde000.phy.2")
    for bus, speed in ((5, "480"), (6, "10000")):
        u = f"{x}/usb{bus}"
        w(r, f"{u}/speed", speed)
        w(r, f"{u}/busnum", str(bus))
        w(r, f"{u}/devnum", "1")
        w(r, f"{u}/power/runtime_status", "active")
        hp = f"{u}/{bus}-0:1.0/usb{bus}-port1"
        w(r, f"{hp}/state", "configured" if bus == 6 else "not attached")
        w(r, f"{hp}/connect_type", "hotplug")
        w(r, f"{hp}/over_current_count", "0")
        w(r, f"{hp}/disable", "MUST NOT BE READ")
        ln(r, f"/sys/bus/usb/devices/usb{bus}", "../../.." + u.removeprefix("/sys"))
    nic = f"{x}/usb6/6-1"
    w(r, f"{nic}/speed", "10000")
    w(r, f"{nic}/idVendor", "0bda")
    w(r, f"{nic}/idProduct", "815a")
    w(r, f"{nic}/busnum", "6")
    w(r, f"{nic}/devnum", "2")
    w(r, f"{nic}/rx_lanes", "1")
    w(r, f"{nic}/power/runtime_status", "suspended")
    w(r, f"{nic}/6-1:1.0/bInterfaceClass", "ff")
    os.makedirs(r + "/sys/bus/usb/drivers/r8152")
    ln(r, f"{nic}/6-1:1.0/driver", "../../../../../../../../bus/usb/drivers/r8152")
    ln(r, "/sys/bus/usb/devices/6-1", "../../.." + nic.removeprefix("/sys"))
    os.makedirs(r + f"{nic}/6-1:1.0/net/eth0")
    w(r, f"{nic}/6-1:1.0/net/eth0/carrier", "0")
    w(r, f"{nic}/6-1:1.0/net/eth0/operstate", "down")
    ln(r, f"{nic}/6-1:1.0/net/eth0/device", "../..")
    ln(r, "/sys/class/net/eth0", "../.." + nic.removeprefix("/sys") + "/6-1:1.0/net/eth0")
    # typec
    tp = "/sys/devices/platform/pmic-glink/pmic_glink.ucsi.0/typec/port1"
    w(r, f"{tp}/data_role", "[host] device")
    w(r, f"{tp}/power_role", "source [sink]")
    w(r, f"{tp}/power_operation_mode", "default")
    w(r, f"{tp}/orientation", "normal")
    os.makedirs(r + f"{tp}/supplier:platform:a800000.usb")
    w(r, f"{tp}/port1.1/svid", "ff01")
    w(r, f"{tp}/port1.1/active", "no")
    w(r, f"{tp}/port1-partner/supports_usb_power_delivery", "yes")
    w(r, f"{tp}/port1-partner/identity/id_header", "0x12345678")
    pd = f"{tp}/port1-partner/pd0"
    w(r, f"{pd}/revision", "3.1")
    w(r, f"{pd}/source-capabilities/1:fixed_supply/voltage", "5000mV")
    w(r, f"{pd}/source-capabilities/1:fixed_supply/maximum_current", "3000mA")
    ln(r, f"{tp}/port1-partner/usb_power_delivery", "pd0")
    ln(r, "/sys/class/typec/port1", "../.." + tp.removeprefix("/sys"))
    # PHYs
    for prov, name, cons in (("fde000.phy", "phy-fde000.phy.2", "a800000.usb"), ("fdd000.phy", "phy-fdd000.phy.17", "a800000.usb"), ("c448000.spmi:pmic@a:phy@fd00", "phy-c448000.spmi:pmic@a:phy@fd00.15", "fdd000.phy")):
        pdir = f"/sys/devices/platform/soc@0/{prov}"
        w(r, f"{pdir}/power/runtime_status", "unsupported")
        os.makedirs(r + f"{pdir}/phy/{name}/consumer:platform:{cons}")
        ln(r, f"/sys/class/phy/{name}", "../.." + pdir.removeprefix("/sys") + f"/phy/{name}")
    # debugfs
    w(r, "/sys/kernel/debug/usb/a800000.usb/regdump", "GCTL = 0x00101001\nGDBGLTSSM = 0x9cc20026\nGUSB2PHYCFG(3) = 0x0")
    w(r, "/sys/kernel/debug/usb/xhci/xhci-hcd.3.auto/ports/port01/portsc", "0x000002a0 Speed=0 Link=RxDetect PP")
    w(r, "/sys/kernel/debug/usb/ucsi/pmic_glink.ucsi.0/vbus_voltage", "0 mV")
    w(r, "/sys/kernel/debug/usb/ucsi/pmic_glink.ucsi.0/command", "WRITE ONLY")
    os.chmod(r + "/sys/kernel/debug/usb/ucsi/pmic_glink.ucsi.0/command", 0o200)
    w(r, "/sys/class/power_supply/ucsi-source-psy-pmic_glink.ucsi.02/uevent", "POWER_SUPPLY_NAME=ucsi-source-psy-pmic_glink.ucsi.02\nPOWER_SUPPLY_ONLINE=1\nOF_NAME=pmic-glink")
    w(r, "/proc/interrupts", fx("interrupts.txt"))
    w(r, "/proc/sys/kernel/random/boot_id", "b00t")
    w(r, "/proc/sys/kernel/osrelease", "7.3.0")
    w(r, "/proc/sys/kernel/hostname", "zenbook-a16")
    ln(r, "/nix/var/nix/profiles/system", "system-65-link")
    w(r, "/dev/kmsg", fx("kmsg.txt"))
    return r


def runner(argv):
    if argv[:2] == ["lsusb", "-t"]:
        return CmdResult(argv, 0, fx("lsusb-t.txt"))
    if argv[0] == "lsusb":
        return CmdResult(argv, 0, "Bus 006 Device 001: ...\n")
    if argv[:2] == ["ip", "-br"]:
        return CmdResult(argv, 0, "eth0 DOWN\n")
    return None


def test_usb_collectors_on_tree(a16):
    ctx = Ctx(root=a16, port="c2", runner=runner)
    res = run_collectors(ctx, "usb")
    bad = {k: v for k, v in res.items() if not v["ok"]}
    assert not bad, bad
    pi = res["usb.ports"]["data"]["c2"]
    assert pi["buses"] == [5, 6] and pi["typec_port"] == "port1" and pi["xhci"] == "xhci-hcd.3.auto"
    assert pi["phys"] == ["fdd000.phy", "fde000.phy"] and pi["repeaters"] == ["c448000.spmi:pmic@a:phy@fd00"]
    tc = res["usb.typec"]["data"]["c2"]
    assert tc["port"]["power_operation_mode"] == "default"
    assert tc["port"]["data_role"] == "host" and tc["port"]["power_role"] == "sink"
    assert tc["port"]["altmodes"]["port1.1"]["svid"] == "ff01"
    assert tc["partner"]["identity"]["id_header"] == "0x12345678"
    assert tc["partner"]["pd"]["source-capabilities"]["1:fixed_supply"]["voltage"] == "5000mV"
    tree = res["usb.tree"]["data"]["c2"]
    port = tree["devices"]["usb6"]["interfaces"]["6-0:1.0"]["ports"]["usb6-port1"]
    assert port["state"] == "configured" and "disable" not in port
    assert tree["devices"]["6-1"]["interfaces"]["6-1:1.0"]["driver"] == "r8152"
    assert tree["lsusb_v"]["usb6"] == "ok"
    assert tree["lsusb_v"]["6-1"].startswith("skipped: runtime_status=suspended")
    ctl = res["usb.controller"]["data"]["c2"]
    assert ctl["dwc3_regs"] == {"GCTL": "0x00101001", "GDBGLTSSM": "0x9cc20026"}
    assert ctl["xhci_portsc"]["port01"]["link"] == "RxDetect"
    ucsi = res["usb.ucsi"]["data"]["pmic_glink.ucsi.0"]
    assert ucsi == {"vbus_voltage": "0 mV"}  # write-only `command` never opened
    nd = res["usb.netdevs"]["data"]["c2"]["eth0"]
    assert nd["carrier"] == "0" and nd["usb_device"] == "6-1" and nd["ethtool"].startswith("skipped")
    assert res["usb.power"]["data"]["supplies"]["ucsi-source-psy-pmic_glink.ucsi.02"] == {"name": "ucsi-source-psy-pmic_glink.ucsi.02", "online": "1"}
    assert res["usb.firmware"]["data"]["c2"]  # rtl8159-1.fw lines on bus 6
    assert res["usb.klog"]["data"]["by_bus"]["6"]["firmware_missing"] >= 2


def test_register_reads_skip_suspended(a16):
    w(a16, "/sys/devices/platform/soc@0/a800000.usb/xhci-hcd.3.auto/power/runtime_status", "suspended")
    ctx = Ctx(root=a16, port="c2", runner=runner)
    res = run_collectors(ctx, "usb")
    ctl = res["usb.controller"]["data"]["c2"]
    assert ctl["dwc3_regs"] == {"skipped": "skipped: xhci-hcd.3.auto runtime_status=suspended"}
    assert ctl["xhci_portsc"]["port01"]["skipped"].startswith("skipped")
    assert {s["path"] for s in ctx.skipped if "path" in s} >= {"/sys/kernel/debug/usb/a800000.usb/regdump"}
    ctx = Ctx(root=a16, port="c2", runner=runner, regs=False)
    assert run_collectors(ctx, "usb")["usb.controller"]["data"]["c2"]["dwc3_regs"]["skipped"].startswith("register reads disabled")


def test_reads_are_confined(a16):
    ctx = Ctx(root=a16)
    with pytest.raises(PermissionError):
        ctx.read("/home/user/.ssh/id_ed25519")


def test_take_and_since_last(a16, tmp_path):
    s = str(tmp_path / "store")
    os.makedirs(s)
    ctx = Ctx(root=a16, port="c2", runner=runner)
    d, snap = snapmain.take(ctx, "usb", "boot present", False, s, 10)
    assert snap["generation"] == 65 and snap["boot_id"] == "b00t" and snap["kernel"] == "7.3.0"
    assert snap["schema"] == 1 and snap["dmesg"]["mode"] == "since_boot"
    files = set(os.listdir(os.path.join(d, "raw")))
    assert {"lsusb-t.txt", "typec.txt", "klog-usb.txt", "dwc3-regdump_c2.txt"} <= files
    with open(os.path.join(d, "snapshot.json")) as f:
        assert json.load(f)["collectors"]["usb.typec"]["ok"]
    with open(os.path.join(s, "index.jsonl")) as f:
        assert json.loads(f.readline())["label"] == "boot present"
    # the next snapshot this boot with --since-last starts after the cursor
    with open(a16 + "/dev/kmsg", "a") as f:
        f.write("3,99999,9000000000,-;usb 6-1: device descriptor read/64, error -71\n")
    ctx = Ctx(root=a16, port="c2", runner=runner)
    d2, snap2 = snapmain.take(ctx, "usb", "after hotplug", True, s, 10)
    assert snap2["dmesg"]["mode"] == "since_last" and snap2["dmesg"]["lines"] == 1
    with open(os.path.join(d2, "dmesg.txt")) as f:
        assert "error -71" in f.read()
    dd = snapdiff.diff(store.load(d), store.load(d2))
    assert dd["changed"]["usb.klog.total.err_71"][1] == dd["changed"]["usb.klog.total.err_71"][0] + 1


def test_redflags_in_every_topic(a16, tmp_path):
    """The redflags collector runs with any topic; a second snapshot this
    boot counts the red flags since the first, and the diff shows them."""
    s = str(tmp_path / "store")
    os.makedirs(s)
    d1, s1 = snapmain.take(Ctx(root=a16, port="c2", runner=runner), "power", "before", False, s, 10)
    rk = s1["collectors"]["redflags.kernel"]
    assert rk["ok"] and rk["data"]["since_last"] is None  # no earlier snapshot this boot
    boot1 = rk["data"]["since_boot"]
    with open(a16 + "/dev/kmsg", "a") as f:
        f.write("3,99990,9000000000,-;qcom-soundwire 6c80000.soundwire: qcom_swrm_irq_handler: SWR bus clash detected\n")
        f.write("3,99991,9000000100,-;wsa884x-codec sdw:1:0:0217:0204:00:1: Parity error detected\n")
        f.write("3,99992,9000000200,-;dwc3-qcom a600000.usb: port-1 HS-PHY not in L2\n")
    d2, s2 = snapmain.take(Ctx(root=a16, port="c2", runner=runner), "audio", "after", False, s, 10)
    rk2 = s2["collectors"]["redflags.kernel"]["data"]
    assert rk2["since_seq"] == s1["dmesg"]["to_seq"]
    assert rk2["since_last"] == {"swr-bus-clash": 1, "wsa-parity": 1, "usb-hsphy-not-l2": 1}
    assert "usb-hsphy-not-l2" not in rk2["since_boot"] and rk2["info"]["usb-hsphy-not-l2"] >= 1
    assert rk2["since_last_by_severity"] == {"crit": 2, "warn": 0, "info": 1}
    sb = rk2["since_boot"]["swr-bus-clash"]
    assert sb["sev"] == "crit" and sb["bead"] == "snapx2ee-bringup-hed" and sb["last"].endswith("SWR bus clash detected")
    assert "swr-bus-clash" not in boot1
    with open(os.path.join(d2, "raw", "redflags.txt")) as f:
        raw = f.read()
    assert "crit swr-bus-clash" in raw and "HS-PHY" not in raw  # info stays out of raw/
    dd = snapdiff.diff(store.load(d1), store.load(d2))
    assert dd["added"]["redflags.kernel.since_boot.swr-bus-clash.count"] == 1
    assert dd["changed"]["redflags.kernel.by_severity.crit"][1] == dd["changed"]["redflags.kernel.by_severity.crit"][0] + 2
    assert not [p for p in [*dd["changed"], *dd["added"]] if ".info" in p or "since_last" in p]  # noise hidden
    assert "redflags.kernel.info.usb-hsphy-not-l2" in snapdiff.diff(store.load(d1), store.load(d2), include_volatile=True)["added"]
    summ = snapmain.summary(d2, s2)
    assert summ["redflags"]["new"]["crit"] == 2
    assert "since the last snapshot: crit 2" in snapmain.redflags_text(summ["redflags"])
