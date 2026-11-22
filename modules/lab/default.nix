{
  config,
  lib,
  pkgs,
  ...
}:

let
  sc = config.lab.zenbook.serialConsole;
in
{
  imports = [
    ./harness.nix
    ./register-nix-paths.nix
    ./boot-healthy.nix
    ./ccache-host.nix
    ./bench.nix
    ./observability.nix
    ./snap.nix
    ./compat.nix
  ];

  hardware.asus-zenbook-a16.kernel.config = {
    NETCONSOLE = "y";
    NETCONSOLE_DYNAMIC = "y";
  }
  // lib.optionalAttrs sc.enable {
    USB_SERIAL = "y";
    USB_SERIAL_CONSOLE = "y";
    USB_SERIAL_FTDI_SIO = "y";
  };

  # `a16-panel off|on`: the panel off through the session's compositor for
  # tests that don't need the screen.
  environment.systemPackages = [ (pkgs.callPackage ../../pkgs/a16-panel { }) ];

  hardware.asus-zenbook-a16.usbWakeup.disabledControllers = lib.mkIf (sc.enable && sc.wakeup) [
    "a400000"
    "a800000"
  ];
}
