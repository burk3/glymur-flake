{ lib, ... }:

# ASUS Zenbook A16 (UX3607OA, Snapdragon X2 Elite / Glymur). Importing this
# module means the system runs on an A16, in the style of nixos-hardware:
# kernel, firmware, device tree, speakers. Optional features (howdy face
# login, IR emitter) have their own switches.
{
  imports = [
    ./kernel.nix
    ./board.nix
    ./firmware.nix
    ./audio
    ./wifi-sleep.nix
    ./edp-retrain.nix
    ./ir-emitter.nix
    ./howdy.nix
  ];

  hardware.asus-zenbook-a16.firmware.enable = lib.mkDefault true;
  hardware.asus-zenbook-a16.audio.enable = lib.mkDefault true;
}
