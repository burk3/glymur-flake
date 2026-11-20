{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.hardware.asus-zenbook-a16.wifi;
  inherit (lib) mkOption types;

  marker = "/run/lab-ath12k-sleep/unloaded";
  kmod = pkgs.kmod;
  log = ''
    log() {
      echo "lab-ath12k-sleep: $*"
      echo "<6>lab-ath12k-sleep: $*" > /dev/kmsg 2>/dev/null || true
    }
  '';
in
{
  options.hardware.asus-zenbook-a16.wifi = {
    unloadAcrossSuspend = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Unload ath12k (ath12k_wifi7, ath12k) before system suspend and
        load it again in the background after resume. A workaround for
        the MHI POWER_ON resume stall in bead snapx2ee-bringup-157, which
        with the systemd hardware watchdog turns into a silent reset.
        Does nothing if ath12k is not loaded when the system suspends.
      '';
    };

    modules = mkOption {
      type = types.listOf types.str;
      default = [
        "ath12k_wifi7"
        "ath12k"
      ];
      description = ''
        Modules to remove before suspend, leaf first. The first one is
        loaded again after resume (it pulls in the rest).
      '';
    };

    unloadTimeout = mkOption {
      type = types.ints.positive;
      default = 20;
      description = ''
        Seconds to wait for the unload before suspending anyway with the
        driver loaded.
      '';
    };
  };

  config = lib.mkIf cfg.unloadAcrossSuspend {
    environment.etc."systemd/system-sleep/lab-ath12k".source =
      pkgs.writeShellScript "lab-ath12k-sleep" ''
        export PATH=${
          lib.makeBinPath [
            pkgs.coreutils
            kmod
            config.systemd.package
          ]
        }
        ${log}
        first=${lib.escapeShellArg (lib.head cfg.modules)}
        case "$1/$2" in
          pre/*)
            rm -f ${marker}
            [ -d "/sys/module/$first" ] || exit 0
            mkdir -p "$(dirname ${marker})"
            # Mark before trying: even a partial unload must be undone.
            touch ${marker}
            t0=$(date +%s%N)
            if timeout -k 5 ${toString cfg.unloadTimeout} modprobe -r ${lib.escapeShellArgs cfg.modules}; then
              log "unloaded ${toString cfg.modules} in $(( ($(date +%s%N) - t0) / 1000000 )) ms before $2"
            else
              rc=$?
              log "unload failed (rc $rc), suspending with ath12k loaded; loaded now: $(cd /sys/module && ls -d ath12k* 2>/dev/null | tr '\n' ' ')"
            fi
            ;;
          post/*)
            [ -e ${marker} ] || exit 0
            rm -f ${marker}
            log "resumed from $2, queueing ath12k reload"
            systemctl start --no-block lab-ath12k-reload.service
            ;;
        esac
        exit 0
      '';

    systemd.services.lab-ath12k-reload = {
      description = "Load ath12k again after resume (bead 157 workaround)";
      restartIfChanged = false;
      path = [
        pkgs.coreutils
        kmod
      ];
      serviceConfig = {
        Type = "oneshot";
        TimeoutStartSec = 90;
      };
      script = ''
        ${log}
        first=${lib.escapeShellArg (lib.head cfg.modules)}
        t0=$(date +%s%N)
        if [ -d "/sys/module/$first" ]; then
          log "$first still loaded (unload failed?), not reloading"
          exit 0
        fi
        if modprobe "$first"; then
          log "loaded $first in $(( ($(date +%s%N) - t0) / 1000000 )) ms"
        else
          log "modprobe $first failed (rc $?)"
          exit 1
        fi
      '';
    };
  };
}
