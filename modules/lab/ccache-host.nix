{ config, lib, ... }:

let
  cfg = config.lab.zenbook.ccacheHost;
  dir = config.lab.zenbook.kernelCcache.dir;
in
{
  options.lab.zenbook.ccacheHost = {
    enable = lib.mkEnableOption "a ccache directory for kernel builds in this host's nix build sandbox";
    maxSize = lib.mkOption {
      type = lib.types.str;
      default = "50G";
      description = "ccache max_size.";
    };
  };

  config = lib.mkIf cfg.enable {
    nix.settings.extra-sandbox-paths = [ dir ];
    systemd.tmpfiles.rules = [
      "d ${dir} 2775 root nixbld -"
      "f+ ${dir}/ccache.conf 0644 root root - umask = 002\\nsloppiness = include_file_mtime,include_file_ctime,time_macros\\nmax_size = ${cfg.maxSize}\\n"
    ];
  };
}
