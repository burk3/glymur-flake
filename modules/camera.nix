{
  config,
  lib,
  pkgs,
  ...
}:

# The cameras: the RGB webcam (OV02C10) and the IR sensor (HM1092) on CAMSS.
# Kernel drivers and DT come with the kernel and board. The RGB webcam needs
# no configuration: libcamera's simple pipeline (with the software ISP)
# drives it and WirePlumber's libcamera monitor publishes it as a PipeWire
# camera, which the camera portal hands to apps (GNOME Snapshot, browsers
# with PipeWire camera support). The IR sensor is for howdy (howdy.nix).
let
  cfg = config.hardware.asus-zenbook-a16.camera;
in
{
  options.hardware.asus-zenbook-a16.camera = {
    tools.enable = lib.mkEnableOption ''
      camera debugging tools: libcamera's `cam` (`cam -l`, captures without
      a desktop) and `qcam`, and v4l-utils (`v4l2-ctl`, `media-ctl` for the
      raw CAMSS media graph)
    '';
  };

  config = lib.mkIf cfg.tools.enable {
    environment.systemPackages = [
      pkgs.libcamera
      pkgs.libcamera-qcam
      pkgs.v4l-utils
    ];
  };
}
