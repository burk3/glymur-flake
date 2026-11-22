{ furmark }:

# nixpkgs' FurMark with its arm64 build fixed: the unpackPhase moves
# FurMark_linux64/ into place on every platform, but the arm64 archive
# unpacks to FurMark_arm64/, so it fails on aarch64. Drop once nixpkgs
# handles the arm64 directory. It unpacks into the build directory, which
# installPhase copies whole, the builder's env-vars included; removed, or
# the output references its source and build tools (~700 MB of closure).
# Unfree (binary-only).
furmark.overrideAttrs (old: {
  unpackPhase = ''
    runHook preUnpack
    7z x $src
    mv FurMark_arm64/* .
    rmdir FurMark_arm64
    runHook postUnpack
  '';
  postInstall = (old.postInstall or "") + ''
    rm -f $out/share/furmark/env-vars
  '';
})
