{
  lib,
  stdenvNoCC,
  fetchurl,
  autoPatchelfHook,
  makeWrapper,
  gcc,
  ocl-icd,
  cacert,
  callPackage,
  fastrpc ? callPackage ../hexagon-fastrpc { },
  dspLibraries ? callPackage ../hexagon-dsp-libs { },
}:

# Upstream's Snapdragon distribution includes the Hexagon v81 code compiled
# with Qualcomm's SDK. Repackage that pinned artifact until the proprietary
# Hexagon SDK/toolchain has a reproducible Nix source-build package.
stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "llama-cpp-hexagon";
  version = "b12023";

  src = fetchurl {
    url = "https://github.com/ggml-org/llama.cpp/releases/download/${finalAttrs.version}/llama-${finalAttrs.version}-bin-linux-arm64-snapdragon.tar.gz";
    hash = "sha256-ENwrLgjA7982PotLljuIOsyKtIjHIhKAMfmvBpQGb1g=";
  };

  nativeBuildInputs = [
    autoPatchelfHook
    makeWrapper
  ];
  buildInputs = [
    gcc.cc.lib
    ocl-icd
  ];
  dontConfigure = true;
  dontBuild = true;
  # The DSP skels are Hexagon ELFs, not Linux aarch64 libraries. They must
  # never be stripped or rewritten by host ELF hooks.
  dontStrip = true;
  dontPatchELF = true;
  dontAutoPatchelf = true;

  installPhase = ''
    runHook preInstall
    mkdir -p "$out"
    cp -a bin lib include LICENSE "$out/"
    mkdir -p "$out/lib/dsp"
    mv "$out"/lib/libggml-htp-*.so "$out/lib/dsp/"
    runHook postInstall
  '';

  postFixup = ''
    # Restrict fixups to the host executable/library directories. Passing
    # --no-recurse prevents visiting lib/dsp and its foreign-architecture ELF.
    autoPatchelf --no-recurse "$out/bin" "$out/lib"
    for executable in "$out"/bin/*; do
      wrapProgram "$executable" \
        --set-default DSPQUEUE_QUEUE_MONITOR 1 \
        --set-default SSL_CERT_FILE "${cacert}/etc/ssl/certs/ca-bundle.crt" \
        --prefix LD_LIBRARY_PATH : "${fastrpc}/lib:$out/lib" \
        --prefix ADSP_LIBRARY_PATH ';' "$out/lib/dsp;${dspLibraries}/lib/dsp" \
        --prefix DSP_LIBRARY_PATH ';' "$out/lib/dsp;${dspLibraries}/lib/dsp"
    done
  '';

  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    test -f "$out/lib/dsp/libggml-htp-v81.so"
    test -f "$out/lib/libggml-hexagon.so"
    "$out/bin/llama-server" --version
    runHook postInstallCheck
  '';

  passthru = { inherit fastrpc dspLibraries; };
  meta = {
    description = "llama.cpp server and tools with the experimental Qualcomm Hexagon backend";
    homepage = "https://github.com/ggml-org/llama.cpp/tree/master/docs/backend/snapdragon";
    license = lib.licenses.mit;
    mainProgram = "llama-server";
    platforms = [ "aarch64-linux" ];
    sourceProvenance = [ lib.sourceTypes.binaryNativeCode ];
  };
})
