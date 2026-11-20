{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.lab.zenbook.observability;
  zb = config.lab.zenbook;
  obs = config.lab.network.observer;
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    types
    ;

  observer = pkgs.callPackage ../../pkgs/a16-observer { };
  telemetry = "${observer}/bin/a16-telemetry";
  target = "${obs.address}:${toString obs.heartbeatPort}";
  hbArgs = lib.escapeShellArgs [
    "--target"
    target
    "--interval"
    (toString zb.heartbeat.interval)
    "--temp-re"
    cfg.heartbeat.tempZones
  ];
  textfileDir = "/run/a16-metrics";
in
{
  imports = [ ./log-shipping.nix ];

  options.lab.zenbook.observability = {
    enable = mkEnableOption "node_exporter, heartbeat v2 to the observer host, sleep markers and boot records (docs/observability-plan.md)";

    lanInterfaces = mkOption {
      type = with types; listOf str;
      default = [
        "enu+"
        "wl+"
      ];
      description = "Interfaces (iptables syntax, + is a wildcard) where the observer host may scrape node_exporter.";
    };

    nodeExporterPort = mkOption {
      type = types.port;
      default = 9100;
    };

    metricsInterval = mkOption {
      type = types.str;
      default = "15s";
      description = "How often the textfile collector runs (timers don't wake the A16 from s2idle).";
    };

    heartbeat.tempZones = mkOption {
      type = types.str;
      default = "^(cpu-[0-2]-[0-5]-0|gpuss-[01]|ddr-0|aoss-0)-thermal$";
      description = ''
        Regex over thermal zone types whose maximum goes into the heartbeat
        (temp_max_mc). TSENS zones only: they are MMIO reads, while the PMIC
        zones are ADC reads over SPMI, too slow to poll every second.
      '';
    };
  };

  config = mkIf cfg.enable {
    lab.zenbook.logShipping.enable = lib.mkDefault true;

    services.prometheus.exporters.node = {
      enable = true;
      port = cfg.nodeExporterPort;
      listenAddress = "0.0.0.0";
      enabledCollectors = [
        "systemd"
        "interrupts"
        "wifi"
      ];
      disabledCollectors = [
        "cpufreq"
        "powersupplyclass"
        "arp"
        "bcache"
        "bonding"
        "btrfs"
        "fibrechannel"
        "infiniband"
        "ipvs"
        "mdadm"
        "nfs"
        "nfsd"
        "rapl"
        "tapestats"
        "xfs"
        "zfs"
      ];
      extraFlags = [ "--collector.textfile.directory=${textfileDir}" ];
    };

    networking.firewall.extraCommands = mkIf (!config.networking.nftables.enable) (
      lib.concatMapStrings (i: ''
        iptables -A nixos-fw -i ${i} -s ${obs.address} -p tcp --dport ${toString cfg.nodeExporterPort} -j nixos-fw-accept
      '') cfg.lanInterfaces
    );
    networking.firewall.extraInputRules = mkIf config.networking.nftables.enable ''
      iifname { ${
        lib.concatMapStringsSep ", " (i: ''"${lib.replaceStrings [ "+" ] [ "*" ] i}"'') cfg.lanInterfaces
      } } ip saddr ${obs.address} tcp dport ${toString cfg.nodeExporterPort} accept
    '';

    systemd.tmpfiles.rules = [
      "d ${textfileDir} 0755 root root -"
      "d /var/lib/a16 0755 root root -"
      "d /var/lib/a16/boots 0755 root root -"
    ];

    systemd.services.a16-metrics = {
      description = "A16 textfile metrics for node_exporter";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${telemetry} metrics --out ${textfileDir}/a16.prom";
        Nice = 10;
      };
    };
    systemd.timers.a16-metrics = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "20s";
        OnUnitActiveSec = cfg.metricsInterval;
        AccuracySec = "1s";
      };
    };

    systemd.services.lab-heartbeat = {
      description = lib.mkForce "Heartbeat v2 (boot_id, uptime, telemetry) to the observer host";
      script = lib.mkForce "exec ${telemetry} heartbeat ${hbArgs}";
      serviceConfig.RuntimeDirectory = "a16-heartbeat";
      serviceConfig.RuntimeDirectoryPreserve = true;
    };

    environment.etc."systemd/system-sleep/a16-mark".source = pkgs.writeShellScript "a16-mark-sleep" ''
      case "$1" in
        pre | post) exec ${telemetry} mark-sleep "$1" ${hbArgs} --state-file /run/a16-heartbeat/state --log /var/lib/a16/sleep.log ;;
      esac
    '';

    systemd.services.a16-redflags = {
      description = "A16 kernel red flag counter for node_exporter";
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-tmpfiles-setup.service" ];
      serviceConfig = {
        ExecStart = "${telemetry} redflags --out ${textfileDir}/redflags.prom --state /run/a16-redflags/state.json";
        RuntimeDirectory = "a16-redflags";
        RuntimeDirectoryPreserve = true;
        Restart = "always";
        RestartSec = "5s";
        Nice = 10;
        ProtectSystem = "strict";
        ReadWritePaths = [ textfileDir ];
        ProtectHome = true;
        PrivateNetwork = true;
        NoNewPrivileges = true;
      };
    };

    systemd.services.a16-boot-record = {
      description = "A16 boot record (identity, raw PMIC PON registers)";
      wantedBy = [ "multi-user.target" ];
      after = [
        "local-fs.target"
        "sys-kernel-debug.mount"
        "systemd-pstore.service"
      ];
      path = [ config.systemd.package ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${telemetry} boot-record";
      };
    };

    environment.systemPackages = [ observer ];
  };
}
