{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.lab.zenbook.bootCounting;
  esp = config.boot.loader.efi.efiSysMountPoint;

  bootCountVar = "/sys/firmware/efi/efivars/LoaderBootCountPath-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f";
in
{
  options.lab.zenbook.bootCounting = {
    tries = lib.mkOption {
      type = lib.types.ints.positive;
      default = 3;
    };
    healthySeconds = lib.mkOption {
      type = lib.types.ints.positive;
      default = 300;
      description = ''
        Uptime required before boot-complete.target is reached and the
        entry is blessed. Boot itself (multi-user.target, logins) does not
        wait for it.
      '';
    };
  };

  config = {
    boot.loader.systemd-boot = {
      enable = true;
      bootCounting = {
        enable = true;
        inherit (cfg) tries;
      };
    };

    systemd.generators.systemd-bless-boot-generator = "/dev/null";

    systemd.services.systemd-bless-boot.unitConfig.ConditionPathExists = bootCountVar;

    systemd.timers.lab-boot-healthy = {
      description = "Mark this boot healthy after ${toString cfg.healthySeconds}s of uptime";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = cfg.healthySeconds;
        AccuracySec = "1s";
      };
    };

    systemd.services.lab-boot-healthy = {
      description = "Mark this boot healthy (boot-complete.target, boot counter)";
      requiredBy = [ "boot-complete.target" ];
      before = [ "boot-complete.target" ];
      wants = [ "systemd-bless-boot.service" ];
      after = [ "local-fs.target" ];
      unitConfig.RequiresMountsFor = [ esp ];
      restartIfChanged = false;
      path = [
        pkgs.coreutils
        pkgs.gnugrep
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        if [ -e ${bootCountVar} ]; then
          echo "LoaderBootCountPath set; systemd-bless-boot blesses the entry"
          exit 0
        fi
        init=$(tr ' ' '\n' < /proc/cmdline | grep '^init=' || true)
        [ -n "$init" ] || { echo "no init= on the cmdline; nothing to bless"; exit 0; }
        shopt -s nullglob
        for e in ${esp}/loader/entries/nixos-*+*.conf; do
          if grep -qF -- "$init" "$e"; then
            mv -v "$e" "''${e%%+*}.conf"
          fi
        done
      '';
    };
  };
}
