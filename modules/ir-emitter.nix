{ config, lib, ... }:

let
  cfg = config.hardware.asus-zenbook-a16.irEmitter;
  dir = ../dts;
  stages = {
    probe = {
      file = dir + "/ir-emitter-probe.dts";
      led = null;
      torchMicroamp = 50000;
    };
    first-light = {
      file = dir + "/ir-emitter.dts";
      led = "ir:flash";
      torchMicroamp = 100000;
    };
    target = {
      file = dir + "/ir-emitter-target.dts";
      led = "ir:flash";
      torchMicroamp = 200000;
    };
  };
  stage = if cfg.stage == null then null else stages.${cfg.stage};
in
{
  options.hardware.asus-zenbook-a16.irEmitter = {
    stage = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum (lib.attrNames stages));
      default = null;
      description = ''
        Which approved IR emitter overlay to apply: `probe` (stage 1, one
        LED per channel), `first-light` (stage 2) or `target` (stage 3).
        null leaves the flash module disabled, as in the base DT.
      '';
    };

    led = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      readOnly = true;
      default = if stage == null then null else stage.led;
      description = "/sys/class/leds name of the ganged IR LED (null: none, or the probe stage's per-channel LEDs).";
    };

    torchMaxMicroamp = lib.mkOption {
      type = lib.types.nullOr lib.types.int;
      readOnly = true;
      default = if stage == null then null else stage.torchMicroamp;
      description = "Torch current at max_brightness (the overlay's led-max-microamp).";
    };
  };

  config = lib.mkIf (cfg.stage != null) {
    hardware.deviceTree.overlays = [
      {
        name = "ir-emitter-${cfg.stage}";
        dtsFile = stage.file;
      }
    ];
  };
}
