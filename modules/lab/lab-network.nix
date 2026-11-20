{ lib, ... }:

let
  inherit (lib) mkOption types;
in
{
  options.lab.network = {
    observer = {
      address = mkOption {
        type = types.str;
        description = "The observer host's address (heartbeat v2, netconsole and journal target).";
      };
      heartbeatPort = mkOption {
        type = types.port;
        default = 7777;
      };
      netconsolePort = mkOption {
        type = types.port;
        default = 6667;
        description = "UDP port of the A16 netconsole sink on the observer host.";
      };
      journalPort = mkOption {
        type = types.port;
        default = 19533;
        description = "systemd-journal-remote in the a16-obs container.";
      };
    };
  };
}
