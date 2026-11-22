{
  config,
  lib,
  ...
}:

# Board settings of the ASUS Zenbook A16 (UX3607OA): kernel command line,
# initrd module set, device tree, and the platform quirks below. Applied
# whenever the A16 kernel is used.
let
  cfg = config.hardware.asus-zenbook-a16;
  usbGlob = "a[${
    lib.concatMapStrings (a: lib.substring 1 1 a) cfg.usbWakeup.disabledControllers
  }]00000";
in
{
  options.hardware.asus-zenbook-a16.usbWakeup.disabledControllers = lib.mkOption {
    type = lib.types.listOf (lib.types.strMatching "a[0-9]00000");
    default = [
      "a400000"
      "a600000"
      "a800000"
    ];
    description = ''
      DWC3 controllers whose USB wakeup is disabled for s2idle. Wake-armed,
      a host-mode controller keeps its PHYs (and with them XO) on through
      suspend; disabled, USB devices re-enumerate on resume. a400000 is the
      USB-A port, a600000 and a800000 the two Type-C ports.
    '';
  };

  config = lib.mkMerge [
    (lib.mkIf cfg.kernel.enable {
      boot.kernelParams = [
        "pd_ignore_unused"
        "cma=128M"
        "asus_glymur_ec.fan_profile=2"

        "msm.separate_gpu_kms=1"

        "deferred_probe_timeout=30"

        "fw_devlink.sync_state=timeout"

        "consoleblank=300"

        "glymur_pci_skip=0"

        "mem_sleep_default=s2idle"
      ];

      boot.kernelModules = [ "dispcc-glymur" ];

      boot.initrd.includeDefaultModules = false;
      boot.initrd.availableKernelModules = [
        "phy-qcom-qmp-pcie"
        "pwrseq-pcie-m2"

        "spmi-pmic-arb"
        "qcom-spmi-pmic"
        "pinctrl-spmi-gpio"
        "fixed"

        "gpucc-glymur"
        "gxclkctl-kaanapali"

        "usb-storage"
        "uas"
        "ums-realtek"

        "hid-generic"
        "hid-asus"
        "hid-multitouch"
      ];

      hardware.deviceTree = {
        enable = true;
        name = "qcom/glymur-asus-zenbook-a16-ux3607oa.dtb";
        overlays = [
          {
            name = "lid-dual-edge";
            dtsFile = ../dts/lid-dual-edge.dts;
          }
          {
            name = "gcc-protected-oob-qup";
            dtsFile = ../dts/gcc-protected-oob-qup.dts;
          }
        ];
      };
      boot.loader.systemd-boot.installDeviceTree = true;
    })
    {
      nixpkgs.hostPlatform = lib.mkDefault "aarch64-linux";

      # rtc-pm8xxx reads the raw PMIC counter and would set the clock to
      # 1970; rtc-qcom-glink (in the patch queue) is the RTC here.
      boot.blacklistedKernelModules = [ "rtc_pm8xxx" ];

      # systemd's TPM2 units stay off unless the system turns them on:
      # without the fTPM driver (tpm.enable) the firmware still reports a
      # TPM and tpm2.target would wait 90 s for /dev/tpm0; with it, they
      # are untested across many boots yet. Never wait for the TPM either way.
      boot.initrd.systemd.tpm2.enable = lib.mkDefault false;
      systemd.tpm2.enable = lib.mkDefault false;
      boot.kernelParams = [ "systemd.tpm2_wait=0" ];

      services.udev.extraRules = lib.mkIf (cfg.usbWakeup.disabledControllers != [ ]) ''
        ACTION=="add", SUBSYSTEM=="platform", KERNEL=="${usbGlob}.usb", ATTR{power/wakeup}="disabled"
      '';
    }
  ];
}
