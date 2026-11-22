{
  lib,
  python3Packages,
  openssh,
  makeWrapper,
  # a16-snap's read-only tools (docs/a16-snap.md); journalctl, nmcli and
  # pw-dump come from the A16's system.
  usbutils,
  ethtool,
  iw,
  edid-decode,
  alsa-utils,
  iproute2,
  util-linux,
  smartmontools,
}:

# A16 observability (docs/observability-plan.md), standard-library Python:
#   a16-obs <serial|heartbeat|netconsole|marks>   services in the a16-obs
#       container on the observer host (modules/a16-obs.nix)
#   a16-telemetry <heartbeat|mark-sleep|metrics|boot-record>   the A16 side
#       (modules/zenbook/observability.nix)
#   a16   the CLI (the observer host via the module, and the dev shell)
#   a16-snap   read-only diagnostic snapshots, run on the A16 (`a16 snap`
#       on the observer host runs it over ssh; docs/a16-snap.md)
# The unit tests (parsers, the serial daemon on a pty, the textfile collector
# and PON dump on a synthetic sysfs tree, CLI helpers, a16-snap's parsers,
# diff, store and collectors on recorded A16 output, the kernel red flag
# patterns on real A16 log lines) run in the build. The
# CLI calls journalctl and lab-lease from the system; ssh is pinned, after
# the system's.
python3Packages.buildPythonApplication {
  pname = "a16-observer";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];
  nativeBuildInputs = [ makeWrapper ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];
  preCheck = ''
    export A16_OBS_JOURNAL_STDOUT=1
  '';

  # One list element per argument (structured attrs don't word-split).
  makeWrapperArgs = [
    "--suffix"
    "PATH"
    ":"
    (lib.makeBinPath [ openssh ])
  ];

  # a16-snap runs its tools by name; pin the ones the A16's system may lack.
  postFixup = ''
    wrapProgram $out/bin/a16-snap --prefix PATH : ${
      lib.makeBinPath [
        usbutils
        ethtool
        iw
        edid-decode
        alsa-utils
        iproute2
        util-linux
        smartmontools
      ]
    }
  '';

  meta = {
    description = "A16 observability: serial daemon, sinks, telemetry and the a16 CLI";
    mainProgram = "a16";
    platforms = lib.platforms.linux;
  };
}
