{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.lab.zenbook;
  net = config.lab.network;
  inherit (lib) mkOption types;
in
{
  imports = [ ./lab-network.nix ];

  options.lab.zenbook = {
    logBufLen = mkOption {
      type = types.nullOr types.str;
      default = "16M";
      description = "log_buf_len= on the cmdline, so early boot is still in the buffer when the log shipper or netconsole replay reads it (null: kernel default).";
    };

    serialConsole = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Kernel console and login on an FTDI USB serial device (the A16 end
          of an FTDI USB-A to USB-A null-modem cable; the observer host owns the
          other end). Built in via lab.zenbook.kernelConfig.
        '';
      };
      device = mkOption {
        type = types.str;
        default = "ttyUSB0";
        description = ''
          Kernel console device (console= on the cmdline). Right at cold boot.
          The kernel console does not survive a re-enumeration of the FTDI
          (a replug, or any resume with `wakeup` off): the usb-serial console
          only re-registers for minor 0, and ttyUSB0 is never freed once a
          getty has opened it, because serial_cleanup() skips its
          usb_serial_put() on a console port (drivers/usb/serial/usb-serial.c,
          v7.3). The FTDI comes back as ttyUSB1 (then alternates 1/2 across
          resumes, as the getty still holds the old one) and the getty
          follows it (see `serial`); read the kernel log there with dmesg.
        '';
      };
      serial = mkOption {
        type = types.nullOr types.str;
        default = null;
        description = ''
          USB serial number of the A16-end FTDI (0403:6001). Its tty, whatever
          ttyUSBn it gets, is linked as /dev/lab-console and gets a getty.
          null matches any FT232R.
        '';
      };
      baud = mkOption {
        type = types.ints.positive;
        default = 115200;
      };
      autologin = mkOption {
        type = types.bool;
        default = true;
        description = "Root autologin on the serial getty (lab cable only; root has no password).";
      };
      wakeup = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Keep the serial console's USB controller (a600000, the left Type-C
          port with the FTDI) wake-armed through s2idle, so the FTDI stays
          enumerated across suspend. Off by default: wake-armed, that
          controller keeps its PHYs and clocks up (no CX collapse) and its HS
          PHY wake (PDC 90) fires within seconds of every s2idle entry
          (beads snapx2ee-bringup-7a8, snapx2ee-bringup-tir). Off, the FTDI
          re-enumerates on every resume: the getty follows it to its new
          ttyUSBn, the kernel console is gone until reboot (see `device`).
          On, the kernel console survives suspend but s2idle doesn't hold.
        '';
      };
    };

    heartbeat.interval = mkOption {
      type = types.ints.positive;
      default = 1;
      description = "Seconds between heartbeat datagrams to the observer host.";
    };
  };

  config = lib.mkMerge [
    {
      boot.kernelParams = [
        "earlycon=efifb"
        "keep_bootcon"
        "panic=10"
        "softlockup_panic=1"
        "hung_task_panic=1"
        "sysctl.kernel.panic_on_rcu_stall=1"
        "irqchip.gicv3_pseudo_nmi=1"
        "nmi_watchdog=1"
      ]
      ++ lib.optional (cfg.logBufLen != null) "log_buf_len=${cfg.logBufLen}"
      ++ lib.optionals cfg.serialConsole.enable [
        "console=tty0"
        "console=${cfg.serialConsole.device},${toString cfg.serialConsole.baud}"
      ];

      systemd.settings.Manager = {
        RuntimeWatchdogSec = lib.mkDefault "30s";
        RebootWatchdogSec = lib.mkDefault "30s";
      };

      services.udev.extraRules =
        let
          serial = lib.optionalString (
            cfg.serialConsole.serial != null
          ) '', ATTRS{serial}=="${cfg.serialConsole.serial}"'';
        in
        lib.mkIf cfg.serialConsole.enable ''
          SUBSYSTEM=="tty", KERNEL=="ttyUSB[0-9]*", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001"${serial}, SYMLINK+="lab-console", TAG+="systemd", ENV{SYSTEMD_WANTS}+="serial-getty@%k.service"
        '';

      boot.loader.efi.canTouchEfiVariables = false;

      systemd.services.lab-heartbeat = {
        description = "Heartbeat (boot_id + uptime) to the observer host";
        wantedBy = [ "multi-user.target" ];
        after = [ "network.target" ];
        path = [ pkgs.socat ];
        serviceConfig = {
          Restart = "always";
          RestartSec = 1;
        };
        script = ''
          boot_id=$(< /proc/sys/kernel/random/boot_id)
          while :; do
            read -r up _ < /proc/uptime
            printf '%s %s\n' "$boot_id" "$up" \
              | socat -u - UDP-SENDTO:${net.observer.address}:${toString net.observer.heartbeatPort} || true
            sleep ${toString cfg.heartbeat.interval}
          done
        '';
      };
    }

    (lib.mkIf (cfg.serialConsole.enable && cfg.serialConsole.autologin) {
      systemd.services = lib.genAttrs (map (n: "serial-getty@ttyUSB${toString n}") (lib.range 0 7)) (_: {
        overrideStrategy = "asDropin";
        serviceConfig.ExecStart = [
          ""
          "${lib.getExe' pkgs.util-linux "agetty"} --login-program ${config.services.getty.loginProgram} --autologin root --noclear %I ${toString cfg.serialConsole.baud} vt220"
        ];
      });
    })
  ];
}
