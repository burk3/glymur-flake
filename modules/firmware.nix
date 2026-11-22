{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.hardware.asus-zenbook-a16.firmware;
  inherit (lib) mkOption types;

  audio = config.hardware.asus-zenbook-a16.audio or null;
  speakerDsp =
    if audio != null && audio.enable && audio.speakers.dsp.enable then
      audio.speakers.dsp.params
    else
      null;

  fw = pkgs.callPackage ../pkgs/a16-firmware {
    inherit (cfg) wifiBoardImage;
    bsp = { inherit (cfg.bsp) name url sha256; };
    inherit speakerDsp;
  };

  dspDir = "qcom/glymur/ASUSTeK/UX3607OA";
  dspFirmware = map (f: "${dspDir}/${f}") [
    "qcadsp8480.mbn"
    "adsp_dtbs.elf"
    "qccdsp8480.mbn"
    "cdsp_dtbs.elf"
    "adspr.jsn"
    "adsps.jsn"
    "adspua.jsn"
  ];
  topology = "qcom/glymur/GLYMUR-ASUS-Zenbook-A16-UX3607OA-tplg.bin";

  dspProviders = [
    "qcom_hwspinlock"
    "qcom_smem"
    "smp2p"
  ];
  dspModules = dspProviders ++ [
    "qcom_aoss"
    "llcc-qcom"
    "qcom_glink_smem"
    "qrtr"
    "qrtr-smd"
    "qcom_pd_mapper"
    "qcom_q6v5_pas"
    "pmic_glink"
    "qcom_battmgr"
  ];
  sharedResetModules = [
    "reset-gpio"
    "gpio-shared-proxy"
  ];
  typecModules = [
    "ucsi_glink"
    "pmic_glink_altmode"
    "aux-hpd-bridge"
  ];
  audioModules = sharedResetModules ++ [
    "snd-soc-x1e80100"
    "snd-soc-wsa884x"
    "soundwire-qcom"
    "pinctrl-sm8650-lpass-lpi"
    "snd-soc-lpass-macro-common"
    "snd-soc-lpass-wsa-macro"
    "snd-soc-lpass-va-macro"
    "snd-soc-lpass-rx-macro"
    "snd-soc-lpass-tx-macro"
    "snd-soc-wcd937x-sdw"
    "q6prm-clocks"
    "q6apm-dai"
    "q6apm-lpass-dais"
  ];

  startDsps = ''
    log() {
      echo "lab-a16-dsp: $*"
      echo "<5>lab-a16-dsp: $*" > /dev/kmsg 2>/dev/null || true
    }
    states() {
      local r n s out=""
      for r in /sys/class/remoteproc/remoteproc*; do
        [ -r "$r/name" ] && [ -r "$r/state" ] || continue
        read -r n < "$r/name"
        read -r s < "$r/state"
        out+="$n:$s "
      done
      echo "''${out:-none}"
    }
    # Bound device count per driver in the chain hwspinlock -> smem -> smp2p
    # -> remoteproc, so a stall shows which provider is missing.
    chain() {
      local d e n out=""
      for d in qcom_hwspinlock qcom-smem qcom_smp2p qcom_q6v5_pas; do
        n=0
        for e in /sys/bus/platform/drivers/$d/*; do
          [ -L "$e" ] && [ -e "$e/driver" ] && n=$((n + 1))
        done
        out+="$d=$n "
      done
      echo "$out"
    }

    for a in $(< /proc/cmdline); do
      if [ "$a" = lab.a16.dsp=off ]; then
        log "lab.a16.dsp=off: ADSP/CDSP stay off this boot"
        exit 0
      fi
    done
    while read -r _ mnt _; do
      if [ "$mnt" = /sysroot ]; then
        log "root is already mounted; refusing to start the DSPs (a USB root could drop)"
        exit 1
      fi
    done < /proc/mounts

    udevadm settle --timeout=30 || log "udev not settled after 30 s; starting the DSPs anyway"

    # Register the speakers' shared reset provider and codec before DSP
    # startup lets SoundWire enumerate.
    for m in ${lib.concatStringsSep " " sharedResetModules} snd_soc_wsa884x; do
      modprobe "$m" || log "could not load $m (audio only)"
    done
    # The remoteprocs' DT-only providers (normally autoloaded by coldplug; made
    # explicit so a missing one fails loudly here).
    for m in ${lib.concatStringsSep " " dspProviders}; do
      modprobe "$m" || log "could not load $m"
    done
    modprobe qcom_pd_mapper || { log "could not load qcom_pd_mapper"; exit 1; }
    modprobe qcom_q6v5_pas || { log "could not load qcom_q6v5_pas"; exit 1; }
    log "modules loaded, waiting up to ${toString cfg.dsp.timeout} s: $(states) [$(chain)]"

    ok=0
    for i in $(seq ${toString cfg.dsp.timeout}); do
      adsp=0 cdsp=0 soccp=0
      for r in /sys/class/remoteproc/remoteproc*; do
        [ -r "$r/name" ] && [ -r "$r/state" ] || continue
        read -r n < "$r/name"
        read -r s < "$r/state"
        case "$n:$s" in
          adsp:running) adsp=1 ;;
          cdsp:running) cdsp=1 ;;
          soccp:attached | soccp:running) soccp=1 ;;
          adsp:crashed | cdsp:crashed | soccp:crashed)
            log "$n crashed: $(states)"
            exit 1
            ;;
        esac
      done
      if [ "$adsp$cdsp$soccp" = 111 ]; then ok=1; break; fi
      if [ $((i % 5)) = 0 ]; then log "''${i} s: $(states) [$(chain)]"; fi
      sleep 1
    done
    if [ "$ok" != 1 ]; then
      log "DSPs not ready after ${toString cfg.dsp.timeout} s: $(states) [$(chain)]"
      exit 1
    fi

    log "ADSP/CDSP running, SoCCP attached: $(states)"

    # Type-C: UCSI and altmode over pmic_glink program the ports' orientation;
    # devices then switch from USB2 to SuperSpeed. Give that a few seconds,
    # then let any USB disconnect/reconnect (from DSP startup or the switch)
    # finish before the root device is checked and mounted.
    tc=""
    for m in ${lib.concatStringsSep " " typecModules}; do
      if modprobe "$m"; then tc+="$m "; else log "could not load $m (Type-C only)"; fi
    done
    log "Type-C modules loaded: ''${tc:-none}; waiting ${toString cfg.dsp.typecDelay} s"
    sleep ${toString cfg.dsp.typecDelay}
    if ! udevadm settle --timeout=30; then
      log "udev not settled 30 s after DSP/Type-C startup: $(states)"
      exit 1
    fi
    usb=""
    for d in /sys/bus/usb/devices/*-*; do
      case ''${d##*/} in *:*) continue ;; esac
      [ -r "$d/speed" ] || continue
      usb+="''${d##*/}@$(< "$d/speed") "
    done
    log "udev settled; typec: $(echo $(ls /sys/class/typec 2>/dev/null)); usb: ''${usb:-none}"
  '';
in
{
  options.hardware.asus-zenbook-a16.firmware = {
    enable = lib.mkEnableOption "the A16's firmware (linux-firmware subset, ASUS BSP firmware, regulatory.db)";

    bsp = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Install the OEM firmware from ASUS's Qualcomm BSP package for the
          UX3607OA (fetched from ASUS's CDN): the ADSP/CDSP images and the
          Wi-Fi board data. Without it only linux-firmware's files are
          installed: no audio/battery DSPs, and ath12k finds no board data
          for this board. (The Bluetooth files: bluetooth.enable.)
        '';
      };
      name = mkOption {
        type = types.str;
        default = "SOCPackage_forWebSite_Qualcomm_Z_V1.312.4500.0_50616.exe";
        description = "File name of ASUS's Qualcomm BSP package (as downloaded).";
      };
      url = mkOption {
        type = types.str;
        default = "https://dlcdnets.asus.com/pub/ASUS/nb/Image/Driver/DriverPackage/50616/SOCPackage_forWebSite_Qualcomm_Z_V1.312.4500.0_50616.exe?model=UX3607OA";
        description = "Download URL of the BSP package (ASUS's UX3607OA support page link).";
      };
      sha256 = mkOption {
        type = types.str;
        default = "e8f2389ac5d4dfd30a1a8481ec3c1e282f24baea2898414351d0b6eb1fa8a0b3";
        description = "sha256 of the BSP package (hex).";
      };
    };

    bluetooth = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Install the Bluetooth patch and NVM the series kernel requests for
          the "Cologne 1.0" controller (qca/clnbtfw10.tlv, qca/clnbtnv10.*),
          extracted from ASUS's Qualcomm BSP .exe (bsp.*).
          Without it BT runs from ROM.
        '';
      };
    };

    wifiBoardImage = mkOption {
      type = types.str;
      default = "QualcommBSP/WIFI_BT/qcwlancol8480/bdwlan_qcc2072_1p0_ncm820A.elf";
      description = ''
        WLAN board image (a path inside the BSP package) put into
        board-2.bin under this board's key. The default reproduces
        jc372/A16Build's verified board-2.bin.
      '';
    };

    fullLinuxFirmware = mkOption {
      type = types.bool;
      default = false;
      description = "Install all of linux-firmware instead of only the A16's subset (larger closure).";
    };

    dsp = {
      mode = mkOption {
        type = types.enum [
          "initrd"
          "stage2"
          "off"
        ];
        default = if cfg.bsp.enable then "initrd" else "off";
        defaultText = lib.literalExpression ''if bsp.enable then "initrd" else "off"'';
        description = ''
          When the ADSP/CDSP start (qcom_q6v5_pas loads).
          - initrd: explicitly, in the initrd before the root device is used,
            then udev settles (see the comment at the top of this module).
          - stage2: udev autoloads it after switch-root. DSP startup can
            disconnect USB devices; do not use with a USB root.
          - off: blacklisted in both stages; no audio, battery or UCSI.
        '';
      };
      timeout = mkOption {
        type = types.ints.positive;
        default = 30;
        description = "Seconds to wait for adsp/cdsp running and soccp attached (progress logged every 5 s).";
      };
      typecDelay = mkOption {
        type = types.ints.unsigned;
        default = 3;
        description = ''
          Seconds to wait after loading the Type-C modules (ucsi_glink,
          pmic_glink_altmode), for devices to re-enumerate at SuperSpeed,
          before udev settles.
        '';
      };
      strict = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Go to the initrd emergency shell if the DSPs don't come up (as
          FixItFoundry's hooks do). Off by default: the boot continues and the
          failure is logged to the kernel log (netconsole).
        '';
      };
      extraInitrdModules = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = ''
          More modules for the early-DSP initrd, e.g. the bridge queue's
          LPASS clock controller or rtc-qcom-glink when that kernel is used.
        '';
      };
    };

    packages = mkOption {
      type = types.attrsOf types.package;
      readOnly = true;
      description = "The firmware derivations, for inspection.";
    };
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        assertions = [
          {
            assertion = cfg.dsp.mode == "off" || cfg.bsp.enable;
            message = "hardware.asus-zenbook-a16.firmware.dsp.mode = \"${cfg.dsp.mode}\" needs the BSP firmware (bsp.enable).";
          }
        ];

        hardware.asus-zenbook-a16.firmware.packages = {
          inherit (fw) linuxFirmwareSubset bspExe;
        }
        // lib.optionalAttrs cfg.bsp.enable { inherit (fw) dsp board2; }
        // lib.optionalAttrs cfg.bluetooth.enable { inherit (fw) bluetooth; };

        hardware.firmware =
          lib.optionals cfg.bsp.enable [
            fw.board2
            fw.dsp
          ]
          ++ lib.optional cfg.bluetooth.enable fw.bluetooth
          ++ [ fw.linuxFirmwareSubset ]
          ++ lib.optional cfg.fullLinuxFirmware pkgs.linux-firmware;
        hardware.wirelessRegulatoryDatabase = true;
      }

      (lib.mkIf (cfg.dsp.mode != "stage2") {
        boot.blacklistedKernelModules = [ "qcom_q6v5_pas" ];
      })

      (lib.mkIf (cfg.dsp.mode == "initrd") {
        boot.initrd.availableKernelModules =
          dspModules ++ audioModules ++ typecModules ++ cfg.dsp.extraInitrdModules;
        boot.initrd.extraFirmwarePaths = dspFirmware ++ [ topology ];

        boot.initrd.systemd.services.lab-a16-dsp = {
          description = "Start the A16 ADSP/CDSP before the root device is used";
          wantedBy = [ "initrd.target" ];
          requiredBy = lib.mkIf cfg.dsp.strict [ "initrd-root-device.target" ];
          after = [
            "systemd-udev-trigger.service"
            "systemd-udevd.service"
            "systemd-modules-load.service"
          ];
          before = [
            "initrd-root-device.target"
            "systemd-fsck-root.service"
            "sysroot.mount"
            "initrd-root-fs.target"
            "initrd.target"
          ];
          unitConfig = {
            DefaultDependencies = false;
            OnFailure = lib.mkIf cfg.dsp.strict [ "emergency.target" ];
            OnFailureJobMode = lib.mkIf cfg.dsp.strict "isolate";
          };
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            TimeoutStartSec = cfg.dsp.timeout + 120;
          };
          script = startDsps;
        };
      })
    ]
  );
}
