{
  lib,
  python3Packages,
  v4l-utils,
  kmod,
}:

# a16-ir-bridge (modules/zenbook/howdy.nix, docs/camera-plan.md): on-demand
# bridge from the HM1092 IR sensor's raw CAMSS path (Y10P) to an 8-bit GREY
# v4l2loopback node for howdy, with auto exposure; plus the PAM-side trigger.
# The unit tests (Y10P unpacking, AE, levels, ioctl layout, trigger) run in
# the build.
python3Packages.buildPythonApplication {
  pname = "a16-ir-bridge";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = [ python3Packages.setuptools ];
  dependencies = [ python3Packages.numpy ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [
      (v4l-utils.override { withGUI = false; })
      kmod
    ])
  ];

  meta = {
    description = "On-demand HM1092 IR to v4l2loopback bridge for howdy on the Zenbook A16";
    mainProgram = "a16-ir-bridge";
    platforms = lib.platforms.linux;
  };
}
