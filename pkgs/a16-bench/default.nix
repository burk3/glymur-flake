{
  lib,
  python3Packages,
  writeText,
  stress-ng,
  _7zz,
  sysbench,
  tinymembench,
  fio,
  iperf3,
  glmark2,
  vkmark,
  util-linux,
}:

# a16-bench (docs/benchmarks.md): milestone benchmarks on the A16 with
# metadata, JSON results and a compare tool. The benchmark tools are pinned
# here (not taken from the system) so runs on different generations use the
# same tool builds; their versions go into every run's metadata. systemd's
# tools (systemd-analyze, systemd-inhibit, systemctl, loginctl) and
# nixos-version come from the running system (/run/current-system/sw/bin).
# The unit tests (tests/, recorded tool output and a synthetic sysfs tree)
# run in the build, so `nix flake check` on x86_64 covers them.
let
  tools = {
    inherit
      stress-ng
      _7zz
      sysbench
      tinymembench
      fio
      iperf3
      glmark2
      vkmark
      ;
  };
  toolVersions = writeText "a16-bench-tool-versions.json" (
    builtins.toJSON (lib.mapAttrs (_: p: p.version or p.name) tools)
  );
in
python3Packages.buildPythonApplication {
  pname = "a16-bench";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];
  dependencies = [ python3Packages.pyyaml ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # One list element per argument: with structured attrs (nixpkgs since
  # 2026-11) an element is no longer word-split.
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath ((lib.attrValues tools) ++ [ util-linux ]))
    "--suffix"
    "PATH"
    ":"
    "/run/current-system/sw/bin"
    "--set"
    "A16_BENCH_TOOL_VERSIONS"
    "${toolVersions}"
  ];

  meta = {
    description = "Milestone benchmarks for the Zenbook A16 (docs/benchmarks.md)";
    mainProgram = "a16-bench";
    platforms = lib.platforms.linux;
  };
}
