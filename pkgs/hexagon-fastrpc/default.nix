{
  lib,
  stdenv,
  fetchFromGitHub,
  autoreconfHook,
  pkg-config,
  libyaml,
  libbsd,
}:

stdenv.mkDerivation {
  pname = "hexagon-fastrpc";
  version = "1.0.8-unstable-2026-10-09";

  src = fetchFromGitHub {
    owner = "qualcomm";
    repo = "fastrpc";
    rev = "a56e9d4de3614a8d8f4b0e15ff561350c4b686e3";
    hash = "sha256-3W2rQxX212hh1nmsyKJTrhoOC5Xb+KfaA/21A1RrX5g=";
  };

  patches = [
    ./nonsecure-eperm.patch
    ./dsp-library-path.patch
    ./queue-monitor.patch
    ./extended-map-flags.patch
  ];

  nativeBuildInputs = [
    autoreconfHook
    pkg-config
  ];
  buildInputs = [
    libyaml
    libbsd
  ];
  enableParallelBuilding = true;
  configureFlags = [
    "--with-config-base-dir=/etc/qcom"
    "--with-systemdsystemunitdir=${placeholder "out"}/lib/systemd/system"
    "--with-udevrulesdir=${placeholder "out"}/lib/udev/rules.d"
    "--with-sysusersdir=${placeholder "out"}/lib/sysusers.d"
  ];

  dontPatchELF = true;
  dontStrip = true;

  postFixup = ''
    for file in "$out"/bin/* "$out"/sbin/*; do
      patchelf --add-rpath "$out/lib" "$file"
    done
  '';

  doCheck = true;
  postCheck = ''
    $CC ${./test-device-fallback.c} -Lsrc/.libs -lcdsprpc \
      -Wl,-rpath,"$PWD/src/.libs" -o test-device-fallback
    ./test-device-fallback
    # Exercise the actual internal search functions without a DSP. Their
    # filesystem/RPC helpers come from the freshly built library.
    sed -n '/^static int try_open_shell_file(/,/^}/p' src/fastrpc_apps_user.c > shell-search.inc
    sed -n '/^static int get_non_preload_lib_names /,/^}/p' src/fastrpc_procbuf.c > lib-names.inc
    $CC ${./test-dsp-library-path.c} -I. -Iinc -Iinc/stddef -Iinc/AEEStdDef \
      -Lsrc/.libs -lcdsprpc -lbsd -Wl,-rpath,"$PWD/src/.libs" -o test-dsp-library-path
    ./test-dsp-library-path
    $CC ${./test-queue-monitor.c} -Iinc -Lsrc/.libs -lcdsprpc \
      -Wl,-rpath,"$PWD/src/.libs" -o test-queue-monitor
    ./test-queue-monitor
  '';
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    test -e "$out/lib/libcdsprpc.so"
    test -e "$out/lib/libcdsp_default_listener.so.1"
    test -x "$out/sbin/cdsprpcd"
    test -x "$out/bin/fastrpc-healthcheck"
    runHook postInstallCheck
  '';

  meta = {
    description = "Qualcomm FastRPC userspace libraries and DSP listeners";
    homepage = "https://github.com/qualcomm/fastrpc";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.linux;
  };
}
