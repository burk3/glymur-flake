{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.lab.a16Obs;
  obs = config.lab.network.observer;
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    types
    ;

  observer = pkgs.callPackage ../pkgs/a16-observer { };
  runDir = "/run/a16-obs";
  devDir = "/dev/a16-obs";
  machineId = builtins.substring 0 32 (builtins.hashString "sha256" "a16-obs container");
  remoteMachineId = builtins.substring 0 32 (builtins.hashString "sha256" "a16-obs remote journal");
  remoteDir = "/var/log/journal/${remoteMachineId}";
  vlUrl = "http://${cfg.victoriaLogs.listenAddress}";

  targetsJson = pkgs.writeText "a16-vmagent-targets.json" (
    builtins.toJSON (
      map (t: {
        targets = [ t.address ];
        labels = {
          inherit (t) nic;
          host = "zenbook-a16";
        };
      }) cfg.vmagent.targets
    )
  );

  hostStateVersion = config.system.stateVersion;
in
{
  imports = [ ./lab/lab-network.nix ];

  options.lab.a16Obs = {
    enable = mkEnableOption "the a16-obs container (A16 serial, heartbeat/netconsole sinks, metrics scrape)";

    a16Hostname = mkOption {
      type = types.str;
      default = "zenbook-a16";
      description = ''
        The A16's networking.hostName, as it appears in its uploaded journal
        (`a16 log`, `a16 boots`, red flags). Metrics keep the fixed label
        host="zenbook-a16", which names the device, not the host.
      '';
    };

    users = mkOption {
      type = with types; listOf str;
      default = [ ];
      description = ''
        Members of the a16-lab group: may use the serial socket (which is a
        root shell on the A16), write marks, pause the scrape and read the
        container journal. Takes effect at their next login.
      '';
    };

    gid = mkOption {
      type = types.int;
      default = 2716;
      description = "GID of a16-lab, the same on the host and in the container (shared socket directory).";
    };

    lanInterfaces = mkOption {
      type = with types; listOf str;
      default = [ ];
      description = "Interfaces where openFirewall opens the A16's UDP ports.";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Open UDP heartbeatPort (7777) and netconsolePort (6667), and TCP
        journalPort (19533) if journalRemote is on, on lanInterfaces in the
        host firewall (the container shares the host's network namespace).
        Off by default so that importing the module does not change the
        firewall by itself.
      '';
    };

    journalRemote = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          systemd-journal-remote on TCP lab.network.observer.journalPort
          (19533; 19532 is the host's own) for the A16's journal upload.
          Plain HTTP, unauthenticated, like the host's for the builder.
        '';
      };
      maxUse = mkOption {
        type = types.str;
        default = "8G";
        description = "journal-remote's cap for the A16 journal files (journal-remote.conf MaxUse=).";
      };
    };

    victoriaLogs = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "VictoriaLogs in the container, ingesting the container journal and the A16 journal.";
      };
      listenAddress = mkOption {
        type = types.str;
        default = "127.0.0.1:9428";
        description = "Host network namespace: keep it on loopback. The `a16` CLI reads A16_VL_URL (default http://127.0.0.1:9428).";
      };
      retentionPeriod = mkOption {
        type = types.str;
        default = "90d";
      };
      maxDiskSpace = mkOption {
        type = types.str;
        default = "20GiB";
        description = "-retention.maxDiskSpaceUsageBytes: oldest days are dropped beyond this.";
      };
      streamFields = mkOption {
        type = with types; listOf str;
        default = [
          "_HOSTNAME"
          "_SYSTEMD_UNIT"
          "SYSLOG_IDENTIFIER"
        ];
        description = "-journald.streamFields.";
      };
    };

    serial = {
      usbSerial = mkOption {
        type = types.str;
        description = "USB serial number of the observer host's FTDI (the host end of the A16 null-modem cable).";
      };
      idVendor = mkOption {
        type = types.str;
        default = "0403";
      };
      idProduct = mkOption {
        type = types.str;
        default = "6001";
      };
      baud = mkOption {
        type = types.ints.positive;
        default = 115200;
      };
    };

    victoriaMetricsUrl = mkOption {
      type = types.str;
      default = "http://127.0.0.1:8428";
      description = "The host's VictoriaMetrics (heartbeat import, vmagent remote write, `a16 metrics`).";
    };

    vmagent = {
      enable = mkOption {
        type = types.bool;
        default = true;
      };
      targets = mkOption {
        type = types.listOf (
          types.submodule {
            options = {
              address = mkOption { type = types.str; };
              nic = mkOption { type = types.str; };
            };
          }
        );
        default = [ ];
        description = "The A16's node_exporter, once per NIC (DHCP reservations); job a16, label nic.";
      };
      scrapeInterval = mkOption {
        type = types.str;
        default = "15s";
      };
      listenAddress = mkOption {
        type = types.str;
        default = "127.0.0.1:8429";
        description = "vmagent's own HTTP listener (host network namespace: keep it on loopback).";
      };
    };

    journal = {
      maxUse = mkOption {
        type = types.str;
        default = "4G";
      };
      maxRetention = mkOption {
        type = types.str;
        default = "180day";
      };
    };

    stateDir = mkOption {
      type = types.str;
      default = "/var/lib/a16-obs";
      description = "Host directory for the container journal (journal/), readable by a16-lab.";
    };

    extraAllowedDevices = mkOption {
      type = with types; listOf attrs;
      default = [ ];
      description = "More containers.a16-obs.allowedDevices entries (tests).";
    };
  };

  config = mkIf cfg.enable {
    users.groups.a16-lab = {
      inherit (cfg) gid;
      members = cfg.users;
    };

    boot.kernelModules = [ "usbserial" ];

    systemd.tmpfiles.rules = [
      "d ${runDir} 2770 root a16-lab -"
      "d ${runDir}/vmagent 2770 root a16-lab -"
      "d ${runDir}/vmagent/active 2770 root a16-lab -"
      "d ${devDir} 0755 root root -"
      "d ${cfg.stateDir} 0750 root a16-lab -"
      "d ${cfg.stateDir}/journal 2755 root systemd-journal -"
      "A+ ${cfg.stateDir}/journal - - - - d:group:a16-lab:r-x,group:a16-lab:r-x"
    ];

    services.udev.extraRules = ''
      ACTION!="remove", SUBSYSTEM=="tty", KERNEL=="ttyUSB[0-9]*", ATTRS{idVendor}=="${cfg.serial.idVendor}", ATTRS{idProduct}=="${cfg.serial.idProduct}", ATTRS{serial}=="${cfg.serial.usbSerial}", TAG+="systemd", ENV{SYSTEMD_WANTS}+="a16-obs-serial-attach@%k.service"
      ACTION=="remove", SUBSYSTEM=="tty", KERNEL=="ttyUSB[0-9]*", RUN+="${pkgs.writeShellScript "a16-obs-serial-detach" ''
        # Drop /dev/a16-obs/console if it is the tty that just went away, so
        # a16-serial can't reopen a minor that another ttyUSB gets next.
        c=${devDir}/console
        [ -c "$c" ] || exit 0
        [ "$((0x$(${pkgs.coreutils}/bin/stat -c %t "$c")))" = "$1" ] || exit 0
        [ "$((0x$(${pkgs.coreutils}/bin/stat -c %T "$c")))" = "$2" ] || exit 0
        ${pkgs.coreutils}/bin/rm -f "$c"
      ''} $env{MAJOR} $env{MINOR}"
    '';

    systemd.services."a16-obs-serial-attach@" = {
      description = "Pass %I (the A16 serial cable) into the a16-obs container";
      path = [
        pkgs.coreutils
        config.systemd.package
      ];
      serviceConfig.Type = "oneshot";
      scriptArgs = "%I";
      script = ''
        dev=/dev/$1
        if [ ! -c "$dev" ]; then
          echo "$dev is gone"
          exit 0
        fi
        maj=$((0x$(stat -c %t "$dev")))
        min=$((0x$(stat -c %T "$dev")))
        mkdir -p ${devDir}
        tmp=${devDir}/.console.$$
        mknod -m 0660 "$tmp" c "$maj" "$min"
        chgrp dialout "$tmp"
        mv -f "$tmp" ${devDir}/console
        echo "$dev ($maj:$min) is ${devDir}/console"
        # a16-serial retries on its own; the HUP just makes it immediate.
        if systemctl -q is-active container@a16-obs.service; then
          systemctl -M a16-obs kill -s HUP a16-serial.service || true
        fi
      '';
    };

    systemd.services."container@a16-obs".preStart = lib.mkBefore ''
      declare root # set by the container unit's environment
      # The bind-mount source must exist before nspawn starts. tmpfiles only
      # creates it at boot (nixos-rebuild switch runs tmpfiles with
      # --exclude-prefix=/dev), and the udev attach only fires on a cable
      # (re)plug, so on a switch with the cable already in, do both here.
      mkdir -p ${devDir}
      ${config.systemd.package}/bin/udevadm trigger --action=add --subsystem-match=tty --sysname-match='ttyUSB*' \
        || true
      mkdir -p "$root/etc"
      if [ ! -s "$root/etc/machine-id" ]; then
        echo ${machineId} > "$root/etc/machine-id"
      fi
    '';

    networking.firewall.interfaces = mkIf cfg.openFirewall (
      lib.genAttrs cfg.lanInterfaces (_: {
        allowedUDPPorts = [
          obs.heartbeatPort
          obs.netconsolePort
        ];
        allowedTCPPorts = lib.optional cfg.journalRemote.enable obs.journalPort;
      })
    );

    environment.systemPackages = [ observer ];
    # The a16 CLI: the A16's hostname (journal and log queries) and its
    # addresses and NIC names, from the scrape targets.
    environment.variables = {
      A16_HOSTNAME = cfg.a16Hostname;
    }
    // lib.optionalAttrs (cfg.vmagent.targets != [ ]) {
      A16_HOSTS = lib.concatMapStringsSep " " (t: lib.head (lib.splitString ":" t.address)) cfg.vmagent.targets;
      A16_HOST_NICS = lib.concatMapStringsSep " " (t: t.nic) cfg.vmagent.targets;
    };

    containers.a16-obs = {
      autoStart = true;
      privateNetwork = false;
      bindMounts = {
        ${runDir} = {
          hostPath = runDir;
          isReadOnly = false;
        };
        ${devDir} = {
          hostPath = devDir;
          isReadOnly = false;
        };
        "/var/log/journal" = {
          hostPath = "${cfg.stateDir}/journal";
          isReadOnly = false;
        };
      };
      allowedDevices = [
        {
          node = "char-ttyUSB";
          modifier = "rw";
        }
      ]
      ++ cfg.extraAllowedDevices;

      config =
        {
          lib,
          options,
          config,
          ...
        }:
        let
          svc = description: args: extra: {
            inherit description;
            wantedBy = [ "multi-user.target" ];
            serviceConfig = {
              ExecStart = "${observer}/bin/a16-obs ${args}";
              User = "a16obs";
              Group = "a16-lab";
              Restart = "always";
              RestartSec = 2;
              LogRateLimitIntervalSec = 0;
              NoNewPrivileges = true;
              ProtectSystem = "strict";
              ProtectHome = true;
              PrivateTmp = true;
              ReadWritePaths = [ runDir ];
            }
            // extra;
          };
        in
        {
          system.stateVersion = hostStateVersion;
          networking.firewall.enable = false;

          users.groups.a16-lab.gid = cfg.gid;
          users.users.a16obs = {
            isSystemUser = true;
            group = "a16-lab";
            extraGroups = [ "dialout" ];
          };

          services.journald =
            let
              j = {
                Storage = "persistent";
                SystemMaxUse = cfg.journal.maxUse;
                MaxRetentionSec = cfg.journal.maxRetention;
              };
            in
            if options.services.journald ? settings then
              { settings.Journal = j; }
            else
              { extraConfig = lib.concatStrings (lib.mapAttrsToList (k: v: "${k}=${v}\n") j); };

          systemd.services = {
            a16-serial =
              svc "A16 serial console owner (the observer host FTDI)"
                "serial --device ${devDir}/console --socket ${runDir}/serial.sock --baud ${toString cfg.serial.baud} --group a16-lab"
                { };
            a16-heartbeat-sink =
              svc "A16 heartbeat sink"
                "heartbeat --port ${toString obs.heartbeatPort} --run-dir ${runDir} --import-url ${cfg.victoriaMetricsUrl}/api/v1/import/prometheus"
                { };
            a16-netconsole-sink =
              svc "A16 netconsole sink" "netconsole --port ${toString obs.netconsolePort}"
                { };
            a16-mark = svc "a16 mark socket" "marks --socket ${runDir}/mark.sock --group a16-lab" { };

            a16-obs-vmagent-targets = lib.mkIf cfg.vmagent.enable {
              description = "vmagent target list for the A16";
              wantedBy = [ "multi-user.target" ];
              before = [ "vmagent.service" ];
              serviceConfig = {
                Type = "oneshot";
                RemainAfterExit = true;
                UMask = "0002";
                Group = "a16-lab";
              };
              script = ''
                d=${runDir}/vmagent
                mkdir -p $d/active
                install -m 0664 ${targetsJson} $d/a16.json.default
                if [ ! -e $d/paused ]; then
                  install -m 0664 ${targetsJson} $d/active/a16.json
                fi
              '';
            };
            vmagent = lib.mkIf cfg.vmagent.enable {
              wants = [ "a16-obs-vmagent-targets.service" ];
              after = [ "a16-obs-vmagent-targets.service" ];
              serviceConfig.SupplementaryGroups = [ "a16-lab" ];
            };

            a16-journal-remote = lib.mkIf cfg.journalRemote.enable {
              description = "A16 journal receiver (systemd-journal-remote)";
              requires = [ "a16-journal-remote.socket" ];
              after = [ "a16-journal-remote.socket" ];
              serviceConfig = {
                ExecStart = "${config.systemd.package}/lib/systemd/systemd-journal-remote --listen-http=-3 --output=${remoteDir}/";
                User = "a16-journal-remote";
                Group = "systemd-journal";
                Restart = "on-failure";
                RestartSec = 2;
                NoNewPrivileges = true;
                ProtectSystem = "strict";
                ProtectHome = true;
                PrivateTmp = true;
                ReadWritePaths = [ remoteDir ];
              };
            };

            victorialogs = lib.mkIf cfg.victoriaLogs.enable {
              startLimitIntervalSec = 0;
            };
          }
          // lib.optionalAttrs cfg.victoriaLogs.enable (
            let
              upload = description: extra: {
                inherit description;
                wantedBy = [ "multi-user.target" ];
                wants = [ "victorialogs.service" ];
                after = [ "victorialogs.service" ];
                startLimitIntervalSec = 0;
                serviceConfig = {
                  ExecStart = "${config.systemd.package}/lib/systemd/systemd-journal-upload --url=${vlUrl}/insert/journald ${extra}";
                  DynamicUser = true;
                  SupplementaryGroups = [ "systemd-journal" ];
                  StateDirectory = "a16-vl-upload";
                  Restart = "always";
                  RestartSec = 5;
                  NoNewPrivileges = true;
                  ProtectSystem = "strict";
                  ProtectHome = true;
                  PrivateTmp = true;
                };
              };
            in
            {
              a16-vl-upload-local = upload "Container journal to VictoriaLogs" "--save-state=/var/lib/a16-vl-upload/local.state";
              a16-vl-upload-a16 = lib.mkIf cfg.journalRemote.enable (
                upload "A16 journal to VictoriaLogs" "--save-state=/var/lib/a16-vl-upload/a16.state --directory=${remoteDir}"
              );
            }
          );

          systemd.sockets.a16-journal-remote = lib.mkIf cfg.journalRemote.enable {
            description = "A16 journal receiver socket";
            wantedBy = [ "sockets.target" ];
            listenStreams = [ (toString obs.journalPort) ];
          };

          users.users.a16-journal-remote = lib.mkIf cfg.journalRemote.enable {
            isSystemUser = true;
            group = "systemd-journal";
          };

          systemd.tmpfiles.rules = lib.optional cfg.journalRemote.enable "d ${remoteDir} 2750 a16-journal-remote systemd-journal -";

          environment.etc."systemd/journal-remote.conf" = lib.mkIf cfg.journalRemote.enable {
            text = ''
              [Remote]
              SplitMode=host
              MaxUse=${cfg.journalRemote.maxUse}
            '';
          };

          services.victorialogs = lib.mkIf cfg.victoriaLogs.enable {
            enable = true;
            inherit (cfg.victoriaLogs) listenAddress;
            extraOptions = [
              "-retentionPeriod=${cfg.victoriaLogs.retentionPeriod}"
              "-retention.maxDiskSpaceUsageBytes=${cfg.victoriaLogs.maxDiskSpace}"
              "-journald.streamFields=${lib.concatStringsSep "," cfg.victoriaLogs.streamFields}"
            ];
          };

          services.vmagent = lib.mkIf cfg.vmagent.enable {
            enable = true;
            remoteWrite.url = "${cfg.victoriaMetricsUrl}/api/v1/write";
            prometheusConfig = {
              scrape_configs = [
                {
                  job_name = "a16";
                  scrape_interval = cfg.vmagent.scrapeInterval;
                  scrape_timeout = "5s";
                  file_sd_configs = [ { files = [ "${runDir}/vmagent/active/*.json" ]; } ];
                }
              ];
            };
            extraArgs = [
              "-httpListenAddr=${cfg.vmagent.listenAddress}"
              "-promscrape.fileSDCheckInterval=5s"
            ];
          };
        };
    };
  };
}
