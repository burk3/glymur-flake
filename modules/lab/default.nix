{ config, lib, ... }:

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

  hardware.asus-zenbook-a16.usbWakeup.disabledControllers = lib.mkIf (sc.enable && sc.wakeup) [
    "a400000"
    "a800000"
  ];
}
