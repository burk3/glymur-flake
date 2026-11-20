{
  config,
  lib,
  options,
  pkgs,
  ...
}:

let
  cfg = config.lab.zenbook.logShipping;
  obs = config.lab.network.observer;
  inherit (lib)
    mkEnableOption
    mkIf
    mkMerge
    mkOption
    types
    ;

  observer = pkgs.callPackage ../../pkgs/a16-observer { };
  telemetry = "${observer}/bin/a16-telemetry";
  ncArgs = lib.escapeShellArgs (
    [
      "--target"
      "${obs.address}:${toString obs.netconsolePort}"
      "--dev-re"
      cfg.netconsole.devices
    ]
    ++ lib.optional cfg.netconsole.replay "--replay"
  );
in
{
  imports = [ ./lab-network.nix ];

  options.lab.zenbook.logShipping = {
    enable = mkEnableOption "journal upload, netconsole and pstore shipping to the observer host";

    syncInterval = mkOption {
      type = types.nullOr types.str;
      default = "30s";
      description = "journald SyncIntervalSec= (null: journald's default, 5 min).";
    };

    journalUpload.enable = mkOption {
      type = types.bool;
      default = true;
    };

    netconsole = {
      enable = mkOption {
        type = types.bool;
        default = true;
      };
      devices = mkOption {
        type = types.str;
        default = "enu.*";
        description = ''
          Regex (full match) of the devices netconsole may use. The USB NIC
          (r8152, enu1 today) only by default: netpoll on Wi-Fi (mac80211,
          ath12k) is doubtful, see docs/observability-plan.md. When the route
          to the observer host goes elsewhere the target is disabled and the journal
          upload carries the kernel log alone.
        '';
      };
      replay = mkOption {
        type = types.bool;
        default = true;
        description = "Send the kernel ring buffer after each (re)attach (the sink drops what it already has).";
      };
    };

    pstore.enable = mkOption {
      type = types.bool;
      default = true;
    };
  };

  config = mkIf cfg.enable (mkMerge [
    (mkIf (cfg.syncInterval != null) (
      if options.services.journald ? settings then
        { services.journald.settings.Journal.SyncIntervalSec = cfg.syncInterval; }
      else
        { services.journald.extraConfig = "SyncIntervalSec=${cfg.syncInterval}\n"; }
    ))

    (mkIf cfg.journalUpload.enable {
      services.journald.upload = {
        enable = true;
        settings.Upload.URL = "http://${obs.address}:${toString obs.journalPort}";
      };
      systemd.services.systemd-journal-upload = {
        startLimitIntervalSec = 0;
        serviceConfig.RestartSec = lib.mkForce "30s";
      };
    })

    (mkIf cfg.netconsole.enable {
      systemd.services.a16-netconsole = {
        description = "netconsole to the observer host (configfs target a16)";
        wantedBy = [ "multi-user.target" ];
        after = [
          "sys-kernel-config.mount"
          "network.target"
          "NetworkManager.service"
        ];
        path = [
          pkgs.iproute2
          pkgs.iputils
        ];
        startLimitIntervalSec = 0;
        serviceConfig = {
          Type = "oneshot";
          ExecStart = "${telemetry} netconsole attach ${ncArgs}";
          Restart = "on-failure";
          RestartSec = 20;
        };
      };

      networking.networkmanager.dispatcherScripts = mkIf config.networking.networkmanager.enable [
        {
          type = "basic";
          source = pkgs.writeShellScript "a16-netconsole-dispatch" ''
            # $1 interface, $2 action. The route to the observer host may have moved.
            case "$2" in
              up | down | dhcp4-change)
                ${config.systemd.package}/bin/systemctl restart --no-block a16-netconsole.service ;;
            esac
          '';
        }
      ];
    })

    (mkIf cfg.pstore.enable {
      systemd.services.a16-pstore-ship = {
        description = "Log new pstore records to the journal (shipped to the observer host)";
        wantedBy = [ "multi-user.target" ];
        after = [
          "systemd-pstore.service"
          "local-fs.target"
        ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${telemetry} pstore-ship";
        };
      };
    })
  ]);
}
