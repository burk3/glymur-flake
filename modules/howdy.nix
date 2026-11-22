{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.hardware.asus-zenbook-a16.howdy;
  bridge = cfg.bridgePackage;
  loopback = "/dev/video${toString cfg.videoNr}";
  howdyPam = "${config.services.howdy.package}/lib/security/pam_howdy.so";
  emitter = config.hardware.asus-zenbook-a16.irEmitter;
  em = cfg.irEmitter;
  emitterOn = em.enable && emitter.led != null;
  emitterBrightness = em.torchMicroamp * 255 / emitter.torchMaxMicroamp;
  emitterOff = pkgs.writeShellScript "a16-ir-emitter-off" ''
    echo 0 > '/sys/class/leds/${emitter.led}/brightness' || true
  '';
  triggerRule = {
    control = "optional";
    modulePath = "${config.security.pam.package}/lib/security/pam_exec.so";
    args = [
      "quiet"
      "${bridge}/bin/a16-ir-bridge-trigger"
    ];
  };
in
{
  options.hardware.asus-zenbook-a16.howdy = {
    enable = lib.mkEnableOption "howdy face authentication through the IR camera bridge";

    pamServices = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [
        "howdy-test"
        "sudo"
      ];
      description = ''
        PAM services (with NixOS' default rules) that try howdy before the
        password. `howdy-test` is a service of its own for checking the stack
        with pamtester (`pamtester howdy-test USER authenticate`). The KDE lock
        screen is `kde`.
      '';
    };

    sddm = lib.mkEnableOption ''
      howdy on the SDDM login screen (sddm's auth is a substack of `login`, so
      howdy goes into sddm's own stack, before it, rather than into `login`)
    '';

    darkThreshold = lib.mkOption {
      type = lib.types.ints.between 0 100;
      default = 95;
      description = ''
        howdy's video.dark_threshold: a frame counts as too dark, and is
        skipped, when more than this percentage of its pixels fall in the
        darkest of eight histogram bins. howdy's own default (60) rejects
        every frame with the IR emitter: the flood LED lights the face but
        not the room behind it, so the dark background alone puts over 60%
        of the frame in that bin ("All frames were too dark") even though
        the bridge's centre-weighted AE exposes the face correctly (centre
        mean 50-63, under 4% clipped, measured 2026-10-27). 95 still skips
        frames that are almost entirely black (nobody there, emitter off in
        the dark).
      '';
    };

    videoNr = lib.mkOption {
      type = lib.types.int;
      default = 42;
      description = "Minor number of the v4l2loopback node the bridge writes (/dev/videoN).";
    };

    bridgePackage = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../pkgs/a16-ir-bridge { };
      defaultText = lib.literalExpression "pkgs.callPackage ../pkgs/a16-ir-bridge { }";
      description = "The a16-ir-bridge package.";
    };

    irEmitter = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = emitter.led != null;
        defaultText = lib.literalExpression "config.hardware.asus-zenbook-a16.irEmitter.led != null";
        description = ''
          Light the IR flood emitter (torch) while the bridge streams, off on
          stop. Needs hardware.asus-zenbook-a16.irEmitter.stage = "first-light" or "target".
          Enrol howdy models with the same emitter state you log in with.
        '';
      };

      torchMicroamp = lib.mkOption {
        type = lib.types.int;
        default = if emitter.torchMaxMicroamp == null then 0 else emitter.torchMaxMicroamp;
        defaultText = lib.literalExpression "config.hardware.asus-zenbook-a16.irEmitter.torchMaxMicroamp";
        description = ''
          Torch current a session starts with, total over the ganged
          channels. At most the stage's approved led-max-microamp (the
          default); written as brightness = floor(torchMicroamp * 255 /
          led-max-microamp). The bridge's auto exposure only ever dims it (to
          1/8 at the least), for a face too close for the shortest exposure.
        '';
      };

      maxSeconds = lib.mkOption {
        type = lib.types.ints.between 1 15;
        default = 15;
        description = ''
          Hard cap on emitter on-time per session; after it the bridge goes on
          with ambient light only. Torch has no hardware timer.
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = lib.optionals em.enable [
      {
        assertion = emitter.led != null;
        message = "hardware.asus-zenbook-a16.howdy.irEmitter needs hardware.asus-zenbook-a16.irEmitter.stage = \"first-light\" or \"target\".";
      }
      {
        assertion =
          emitter.torchMaxMicroamp != null
          && em.torchMicroamp > 0
          && em.torchMicroamp <= emitter.torchMaxMicroamp;
        message = "hardware.asus-zenbook-a16.howdy.irEmitter.torchMicroamp must be in 1..the stage's approved torch current (${toString emitter.torchMaxMicroamp} uA).";
      }
    ];

    boot.extraModulePackages = [ config.boot.kernelPackages.v4l2loopback ];
    boot.extraModprobeConfig = ''
      options v4l2loopback video_nr=${toString cfg.videoNr} card_label="A16 IR" exclusive_caps=1 max_buffers=2
    '';

    services.howdy = {
      enable = true;
      control = "sufficient";
      package = pkgs.howdy.overrideAttrs (old: {
        passthru = old.passthru // {
          pythonDeps = map (d: if d == "opencv4Full" then "opencv4" else d) old.passthru.pythonDeps;
        };
      });
      settings = {
        core = {
          detection_notice = true;
        };
        video = {
          device_path = loopback;
          timeout = 5;
          dark_threshold = cfg.darkThreshold;
          max_height = 360;
        };
      };
    };
    security.pam.howdy.enable = false;

    security.pam.services = lib.mkMerge [
      (lib.genAttrs cfg.pamServices (name: {
        howdy.enable = true;
        rules.auth.a16-ir-bridge = triggerRule // {
          order = config.security.pam.services.${name}.rules.auth.howdy.order - 10;
        };
      }))
      { howdy-test = { }; }
      (lib.mkIf cfg.sddm {
        sddm.rules.auth = {
          a16-ir-bridge = triggerRule // {
            order = config.security.pam.services.sddm.rules.auth.login.order - 20;
          };
          howdy = {
            control = "sufficient";
            modulePath = howdyPam;
            order = config.security.pam.services.sddm.rules.auth.login.order - 10;
          };
        };
      })
    ];

    systemd.sockets.a16-ir-bridge = {
      description = "A16 IR camera bridge (howdy) activation socket";
      wantedBy = [ "sockets.target" ];
      socketConfig = {
        ListenStream = "/run/a16-ir-bridge.sock";
        SocketMode = "0666";
        Accept = false;
      };
    };
    systemd.services.a16-ir-bridge = {
      description = "A16 IR camera bridge for howdy (HM1092 -> ${loopback})";
      requires = [ "a16-ir-bridge.socket" ];
      startLimitIntervalSec = 30;
      startLimitBurst = 30;
      serviceConfig = {
        Type = "simple";
        ExecStart = lib.concatStringsSep " " (
          [
            (lib.getExe bridge)
            "serve"
            "--loopback ${loopback}"
          ]
          ++ lib.optionals emitterOn [
            "--emitter-led ${emitter.led}"
            "--emitter-brightness ${toString emitterBrightness}"
            "--emitter-seconds ${toString em.maxSeconds}"
          ]
        );
        ExecStopPost = lib.mkIf emitterOn "${emitterOff}";
        RuntimeDirectory = "a16-ir-bridge";
        RuntimeDirectoryPreserve = true;
        TimeoutStopSec = 5;
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
      };
    };

    # A howdy login starts the user's WirePlumber while the bridge still
    # streams (it stops ~2 s after howdy closes the node). WirePlumber's
    # libcamera monitor enumerates the cameras once, at start: the simple
    # pipeline's setupLinks() then cannot switch csid0 away from the busy IR
    # route ("Failed to setup link 'msm_csiphy0'[1] -> 'msm_csid0'[0]: Device
    # or resource busy"), drops both cameras, and never retries, so the
    # session has no PipeWire camera (bead snapx2ee-bringup-dm8). Hold
    # WirePlumber's start until the bridge has restored the RGB route, at
    # most 20 s; it starts anyway after that.
    systemd.user.services.wireplumber.serviceConfig.ExecStartPre = lib.mkIf config.services.pipewire.wireplumber.enable [
      "-${pkgs.writeShellScript "wait-a16-ir-bridge" ''
        for ((i = 0; i < 100; i++)); do
          ${config.systemd.package}/bin/systemctl is-active --quiet a16-ir-bridge.service || exit 0
          ${pkgs.coreutils}/bin/sleep 0.2
        done
        echo "a16-ir-bridge still active after 20 s; starting anyway" >&2
      ''}"
    ];

    environment.systemPackages = [
      bridge
      pkgs.pamtester
    ];
  };
}
