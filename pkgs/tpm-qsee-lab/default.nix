# The staged qcom.tz.tpm QSEE TPM transport (tpm_qsee_lab.c) as an
# out-of-tree module for a given A16 kernel; modules/tpm.nix loads it.
{
  lib,
  stdenv,
  kernel,
}:
let
  # nixpkgs since 2026-11 signs modules with kmodigest (a single-module
  # certificate next to each .ko, merged into modules.cer) and its module
  # aggregation (system.modulesTree) expects modules.cer and a .ko.cer for
  # every external module. A kernel built with an older nixpkgs (e.g. a
  # pinned hardware.asus-zenbook-a16.kernel.pkgs) has neither, so then this
  # package ships empty ones; with a kmodigest kernel it signs the module the
  # way Kbuild's modules_install would (cmd_sign from kernel.makeFlags).
  signFlag = lib.findFirst (lib.hasPrefix "cmd_sign=") null (kernel.makeFlags or [ ]);
  kmodigest =
    if signFlag == null then
      null
    else
      lib.removeSuffix "$(space)$@" (lib.removePrefix "cmd_sign=" signFlag);
  modDir = "$out/lib/modules/${kernel.modDirVersion}";
in
stdenv.mkDerivation {
  pname = "tpm-qsee-lab";
  version = "0.1-${kernel.version}";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./Kbuild
      ./tpm_qsee_lab.c
    ];
  };
  nativeBuildInputs = kernel.moduleBuildDependencies;
  # kernel.makeFlags carries linuxManualConfig's own build-root flags, which
  # break an external-module build; only ARCH is needed here.
  buildPhase = ''
    runHook preBuild
    make -C ${kernel.dev}/lib/modules/${kernel.modDirVersion}/build \
      M="$PWD" ARCH=${stdenv.hostPlatform.linuxArch} modules
    runHook postBuild
  '';
  installPhase = ''
    install -Dm644 tpm_qsee_lab.ko ${modDir}/extra/tpm_qsee_lab.ko
  ''
  + (
    if kmodigest != null then
      ''
        ${kmodigest} ${modDir}/extra/tpm_qsee_lab.ko
      ''
    else
      ''
        touch ${modDir}/extra/tpm_qsee_lab.ko.cer ${modDir}/modules.cer
      ''
  );
}
