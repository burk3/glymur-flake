{ config, lib, ... }:

let
  cfg = config.hardware.asus-zenbook-a16.tpm;
  mod = config.boot.kernelPackages.callPackage ../pkgs/tpm-qsee-lab { };
in
{
  options.hardware.asus-zenbook-a16.tpm = {
    enable = lib.mkEnableOption "the fTPM over QSEECOM (tpm_qsee_lab, /dev/tpm0)";
    stage = lib.mkOption {
      type = lib.types.ints.between 0 3;
      default = 3;
      description = ''
        tpm_qsee_lab stage=: 3 registers the TPM (/dev/tpm0, /dev/tpmrm0);
        0-2 are diagnostics only (app lookup, QUERY_INFO_2, raw read-only
        commands without a TPM device).
      '';
    };
    blacklistQcomtee = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Keep qcomtee from loading. Nothing on this firmware uses it (no
        QCOMTEE services are registered), and its probe looks up the TPM's
        QTEE service while this driver owns the fTPM.
      '';
    };
    controlArea = lib.mkOption {
      type = lib.types.str;
      default = "0x81d10000";
      description = ''
        ca_allow=: the only control area address the module maps (the one
        the fTPM reports in QUERY_INFO_2 on the UX3607OA); a different
        report makes the module refuse to map it.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    boot.extraModulePackages = [ mod ];
    boot.initrd.kernelModules = [ "tpm_qsee_lab" ];
    boot.blacklistedKernelModules = lib.optional cfg.blacklistQcomtee "qcomtee";
    boot.kernelParams = [
      "tpm_qsee_lab.stage=${toString cfg.stage}"
      "tpm_qsee_lab.ca_allow=${cfg.controlArea}"
    ];
  };
}
