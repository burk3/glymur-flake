{
  config,
  lib,
  pkgs,
  inputs,
  ...
}:

let
  cfg = config.lab.zenbook.bench;
in
{
  options.lab.zenbook.bench = {
    enable = lib.mkEnableOption "the a16-bench milestone benchmark suite (docs/benchmarks.md)";
    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../../pkgs/a16-bench { };
      defaultText = lib.literalExpression "pkgs.callPackage ../../pkgs/a16-bench { }";
      description = "The a16-bench package.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
    systemd.tmpfiles.rules = [ "d /var/lib/a16-bench 0755 root root -" ];
    system.configurationRevision = lib.mkDefault (inputs.self.rev or inputs.self.dirtyRev or null);
  };
}
