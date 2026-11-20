{
  _7zz,
  alsa-utils,
  lib,
  fetchFromGitHub,
  fetchurl,
  gnum4,
  jq,
  linux-firmware,
  python3,
  requireFile,
  runCommand,
  unzip,
  extract ? {
    name = "a16-firmware-20261022-140341.zip";
    sha256 = "ca1597bb652232a458e0e1ead0a1318063a44a9b260c9ed25384833ecb90ba6c";
  },
  wifiBoardImage ? "qcwlancol8480.inf_arm64_d440e12aca6ddc77/bdwlan_qcc2072_1p0_ncm820A.elf",
  bsp ? {
    name = "SOCPackage_forWebSite_Qualcomm_Z_V1.312.4500.0_50616.exe";
    url = "https://dlcdnets.asus.com/pub/ASUS/nb/Image/Driver/DriverPackage/50616/SOCPackage_forWebSite_Qualcomm_Z_V1.312.4500.0_50616.exe?model=UX3607OA";
    sha256 = "e8f2389ac5d4dfd30a1a8481ec3c1e282f24baea2898414351d0b6eb1fa8a0b3";
  },
  speakerDsp ? null,
}:

let
  inherit (lib) escapeShellArg;

  zip = requireFile {
    inherit (extract) name sha256;
    meta.license = lib.licenses.unfreeRedistributableFirmware;
    message = ''
      The A16 firmware extract ${extract.name} is not in the Nix store.
      It is OEM firmware and is never committed to the repository; add your
      own copy (made by firmware/extract-windows-a16.ps1 on the A16's Windows
      install) to the local store:

        nix-store --add-fixed sha256 /path/to/${extract.name}
      or
        nix store add --mode flat --hash-algo sha256 /path/to/${extract.name}

      Remote builders get it from the local store as a build input. A newer
      extract: set hardware.asus-zenbook-a16.firmware.extract.{name,sha256} to match it.
    '';
  };

  bspExe = fetchurl {
    inherit (bsp) name url sha256;
    meta.license = lib.licenses.unfreeRedistributableFirmware;
  };

  btSha256 = {
    "clnbtfw10.tlv" = "0193aafabd279d464a415c5b8bed39f93a56dadd373a04efb31ab22067b65828";
    "clnbtnv10.bin" = "8dcf11416f6beefc155c926617e094a86731a9ba3ec91daf6ae6979cfd27da8b";
    "clnbtnv10.b03" = "8dcf11416f6beefc155c926617e094a86731a9ba3ec91daf6ae6979cfd27da8b";
    "clnbtnv10.b07" = "8dcf11416f6beefc155c926617e094a86731a9ba3ec91daf6ae6979cfd27da8b";
    "clnbtnv10.b08" = "8dcf11416f6beefc155c926617e094a86731a9ba3ec91daf6ae6979cfd27da8b";
    "clnbtnv10.b0a" = "8dcf11416f6beefc155c926617e094a86731a9ba3ec91daf6ae6979cfd27da8b";
    "clnbtnv10.b0d" = "8dcf11416f6beefc155c926617e094a86731a9ba3ec91daf6ae6979cfd27da8b";
    "clnbtnv10.b17" = "5d136624e4738057d18c2f3e27086938c85c1e1c7d0c678f40c7e1fd1bbea182";
  };
  btPkgDir = "QualcommBSP/WIFI_BT/qcbluetooth8480";

  dspSha256 = {
    "qcadsp8480.mbn" = "67b4e129d4d60fccd05d85be3754168f46535549a03a3627c598d634e73df49d";
    "qccdsp8480.mbn" = "acf13d29288d283793f418a36650e8a261eed927d30de109a59cb2210f458794";
    "adsp_dtbs.elf" = "7906466c734b2f360d48cf086f80b6e04d84e9b02255c74545aa28d5df1e932c";
    "cdsp_dtbs.elf" = "9bee104a48d4aac647287d6c1454df170c1cff7158a2352711fd1e2163ea6d1d";
  };

  unpack = ''
    unzip -q ${zip} -d extract || [ $? -eq 1 ]
    chmod -R u+rwX extract
    (cd extract && sha256sum --quiet -c SHA256SUMS)
  '';

  dspDir = "qcom/glymur/ASUSTeK/UX3607OA";

  bdencoder = fetchurl {
    url = "https://raw.githubusercontent.com/qca/qca-swiss-army-knife/6df4dae3e2f5e4c2903f3cafd40996fc1b3639ce/tools/scripts/ath12k/ath12k-bdencoder";
    hash = "sha256-/cyNyWKNZ+UA1Jah3iLoLhNt3q7DJmnqGzrdk/KYBlI=";
  };

  lf = "${linux-firmware}/lib/firmware";

  audioreachTopology = fetchFromGitHub {
    owner = "linux-msm";
    repo = "audioreach-topology";
    rev = "d6bb205aca5f4b8fa6b2f44cf8b5f6a081cbf037";
    hash = "sha256-J/xqKMa428ScOa/WOvoRopZpo2L70w78XRp8v7JHKRY=";
  };
  speakerDspParams = builtins.toJSON speakerDsp;
  a16Topology =
    runCommand "a16-audioreach-topology"
      {
        nativeBuildInputs = [
          alsa-utils
          gnum4
          python3
        ];
        inherit speakerDspParams;
        passAsFile = [ "speakerDspParams" ];
        passthru.report = runCommand "a16-speaker-dsp-report" {
          nativeBuildInputs = [ python3 ];
          inherit speakerDspParams;
          passAsFile = [ "speakerDspParams" ];
        } "python3 ${./topology/speaker-dsp-blobs.py} --report $speakerDspParamsPath > $out";
      }
      ''
        python3 ${./topology/speaker-dsp-blobs.py} $speakerDspParamsPath > speaker-dsp-blobs.m4
        m4 -I ${audioreachTopology} -I ${./topology} -I . -DA16_SPEAKER_DSP \
          ${./topology/GLYMUR-ASUS-Zenbook-A16-UX3607OA.m4} > a16.conf
        # Routes to the kernel's BE widgets are reported as undefined; they
        # are resolved when the topology loads (same for upstream's files).
        alsatplg -c a16.conf -o a16-tplg.bin
        install -Dm444 a16-tplg.bin $out
      '';
in
{
  inherit zip bspExe;
  speakerDspTopology = if speakerDsp == null then null else a16Topology;

  bluetooth =
    runCommand "a16-firmware-bluetooth"
      {
        nativeBuildInputs = [ _7zz ];
      }
      ''
        7zz x -y -bso0 -bsp0 -obsp ${bspExe} '${btPkgDir}/clnbt*'
        cd bsp/${btPkgDir}
        ${lib.concatStrings (
          lib.mapAttrsToList (f: h: ''
            echo "${h}  ${f}" | sha256sum --quiet -c
          '') btSha256
        )}
        install -Dm444 -t $out/lib/firmware/qca ${lib.concatStringsSep " " (lib.attrNames btSha256)}
      '';

  windows =
    runCommand "a16-firmware-windows"
      {
        nativeBuildInputs = [ unzip ];
        passthru.compressFirmware = false;
      }
      ''
        ${unpack}
        cd extract
        ${lib.concatStrings (
          lib.mapAttrsToList (f: h: ''
            echo "${h}  ${dspDir}/${f}" | sha256sum --quiet -c
          '') dspSha256
        )}
        install -Dm444 -t $out/lib/firmware/${dspDir} \
          ${dspDir}/{qcadsp8480.mbn,qccdsp8480.mbn,adsp_dtbs.elf,cdsp_dtbs.elf,adspr.jsn,adsps.jsn,adspua.jsn}
        # Bluetooth patch/NVM for the WCN7850 (hmt) naming. The series kernel
        # identifies this chip as "Cologne 1.0" and asks for qca/clnbtfw10.tlv
        # instead, which the extract and linux-firmware lack (see bluetooth).
        install -Dm444 -t $out/lib/firmware/qca qca/hmt*
      '';

  board2 =
    runCommand "a16-qcc2072-board-2"
      {
        nativeBuildInputs = [
          unzip
          jq
          python3
        ];
      }
      ''
        ${unpack}
        mkdir work && cd work
        cp ${lf}/ath12k/QCC2072/hw1.0/board-2.bin board-2.bin
        chmod u+w board-2.bin
        python3 ${bdencoder} -e board-2.bin > /dev/null
        cp ../extract/windows-packages/wlan/${escapeShellArg wifiBoardImage} a16.bin

        # The BIOS reports the A14's variant (UX3407Q; kernel.org bug 221984),
        # so match with and without it. Drop any existing entry for this board.
        key="bus=pci,vendor=17cb,device=1112,subsystem-vendor=105b,subsystem-device=e14f,qmi-chip-id=33,qmi-board-id=255"
        jq --arg k "$key" '
          .[0].board |= ([ .[] | select(any(.names[]; contains("subsystem-device=e14f")) | not) ]
            + [ { names: [ ($k + ",variant=UX3407Q"), $k ], data: "a16.bin" } ])
        ' board-2.json > board-2.json.new
        mv board-2.json.new board-2.json
        rm board-2.bin
        python3 ${bdencoder} -c board-2.json > /dev/null

        # Check the result by extracting it again.
        mkdir verify && cp board-2.bin verify/ && cd verify
        python3 ${bdencoder} -e board-2.bin > /dev/null
        jq -e --arg k "$key" '[ .[0].board[].names[] | select(. == $k) ] | length == 1' board-2.json
        cd ..

        install -Dm444 board-2.bin $out/lib/firmware/ath12k/QCC2072/hw1.0/board-2.bin
        # jc372/A16Build's verified build from the same inputs is
        # 314e2d5702bd5431bd7abc6f71c6610f79f79ece50c5f854ccadeaee1bb27b49
        # (linux-firmware 20260911/20260916 container + the ncm820A image).
        mkdir -p $out/share/a16-firmware
        {
          echo "image: ${wifiBoardImage}"
          echo "container: ${linux-firmware.name}"
          sha256sum board-2.bin
        } > $out/share/a16-firmware/board-2.txt
      '';

  linuxFirmwareSubset = runCommand "a16-linux-firmware-subset" { } ''
    fw=$out/lib/firmware
    copy() { install -Dm444 "${lf}/$1" "$fw/''${2:-$1}"; }

    # Adreno X2 (a6xx_catalog: gen80100); zap image for completeness (the
    # A16 DT has no zap-shader node, so it is not requested today).
    for f in gen80100_sqe.fw gen80100_gmu.bin gen80100_aqe.fw; do copy qcom/$f; done
    copy qcom/glymur/gen80100_zap.mbn

    # Wi-Fi: firmware-2.bin (ath12k only falls back to amss.bin/m3.bin
    # without it). board-2.bin comes from board2 when the extract is used.
    for f in firmware-2.bin board-2.bin Notice.txt; do copy ath12k/QCC2072/hw1.0/$f; done

    # Bluetooth (WCN7850 hmt naming); the cln10 files come from bluetooth.
    for f in ${lf}/qca/hmtbtfw20.tlv ${lf}/qca/hmtnv20.*; do copy "qca/''${f##*/}"; done

    # r8152 USB NICs (RTL8153/8156, a few KiB). rtl8159-1.fw (the lab's
    # 10G adapter, 0bda:815a) is in no linux-firmware release: Realtek has
    # not licensed it for redistribution; without it the RTL8159 links at
    # 5 Gbit/s at most.
    for f in ${lf}/rtl_nic/rtl815*; do copy "rtl_nic/''${f##*/}"; done

    # AudioReach topology. Upstream linux-firmware added the A16's on
    # 2026-10-22 (MR !1309): byte-identical to GLYMUR-CRD-tplg.bin, which this
    # linux-firmware already has; with `speakerDsp`, ours (a16Topology
    # above). Same layout as upstream's WHENCE (file under ASUSTeK/UX3607OA,
    # link at the name snd-x1e80100 requests).
    copy qcom/glymur/GLYMUR-CRD-tplg.bin
    ${
      if speakerDsp == null then
        "copy qcom/glymur/GLYMUR-CRD-tplg.bin qcom/glymur/ASUSTeK/UX3607OA/GLYMUR-ASUS-Zenbook-A16-UX3607OA-tplg.bin"
      else
        "install -Dm444 ${a16Topology} $fw/qcom/glymur/ASUSTeK/UX3607OA/GLYMUR-ASUS-Zenbook-A16-UX3607OA-tplg.bin"
    }
    ln -s ASUSTeK/UX3607OA/GLYMUR-ASUS-Zenbook-A16-UX3607OA-tplg.bin \
      $fw/qcom/glymur/GLYMUR-ASUS-Zenbook-A16-UX3607OA-tplg.bin
  '';
}
