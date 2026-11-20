{
  lib,
  runCommand,
  fetchurl,
  _7zz,
  bsp ? {
    name = "SOCPackage_forWebSite_Qualcomm_Z_V1.312.4500.0_50616.exe";
    url = "https://dlcdnets.asus.com/pub/ASUS/nb/Image/Driver/DriverPackage/50616/SOCPackage_forWebSite_Qualcomm_Z_V1.312.4500.0_50616.exe?model=UX3607OA";
    sha256 = "e8f2389ac5d4dfd30a1a8481ec3c1e282f24baea2898414351d0b6eb1fa8a0b3";
  },
}:

let
  archive = fetchurl {
    inherit (bsp) name url sha256;
    meta.license = lib.licenses.unfreeRedistributableFirmware;
  };
in
runCommand "a16-hexagon-dsp-libs-00304"
  {
    nativeBuildInputs = [ _7zz ];
    passthru.firmwareVersion = "CDSP.HT.3.3-00304-GLYMUR-1";
    meta = {
      description = "ASUS A16 firmware-matched CDSP FastRPC shells and C++ runtime";
      license = lib.licenses.unfreeRedistributableFirmware;
      platforms = lib.platforms.linux;
    };
  }
  ''
    7zz x -y ${archive} 'QualcommBSP/NPU/qcnspmcdm8480/CDSP/*' \
      'QualcommBSP/NPU/qcnspmcdm8480/qccdsp8480.mbn' > /dev/null
    cd QualcommBSP/NPU/qcnspmcdm8480
    # Fail rather than silently pair another BSP/firmware with these shells.
    sha256sum --check <<'HASHES'
    acf13d29288d283793f418a36650e8a261eed927d30de109a59cb2210f458794  qccdsp8480.mbn
    61301ad82965b291d868989bf224ebe464501d38ea786c8f4ef3231eea6f22d5  CDSP/fastrpc_shell_3
    b65b25e7c399f5272920e5d2b9f3197164b9eb1818194cb44339bc89f603b1f8  CDSP/fastrpc_shell_unsigned_3
    3cabc7b43a32901d1ce04f8c6fdebf4ff5eda7d16821c3e82f20ccacd393de25  CDSP/libc++.so.1
    dcf003c35191ec093af4837a81e03030edad8144fababe204b0603657bd6e145  CDSP/libc++abi.so.1
    HASHES
    mkdir -p "$out/lib/dsp"
    cp CDSP/fastrpc_shell_3 CDSP/fastrpc_shell_unsigned_3 \
      CDSP/libc++.so.1 CDSP/libc++abi.so.1 "$out/lib/dsp/"
  ''
