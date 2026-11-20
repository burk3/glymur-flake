{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.lab.zenbook.snap;
in
{
  options.lab.zenbook.snap = {
    enable = lib.mkEnableOption "a16-snap, read-only diagnostic snapshots (docs/a16-snap.md)";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../../pkgs/a16-observer { };
      defaultText = lib.literalExpression "pkgs.callPackage ../../pkgs/a16-observer { }";
      description = "The package providing a16-snap (a16-observer).";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
    systemd.tmpfiles.rules = [ "d /var/lib/a16-snap 0700 root root -" ];
  };
}
