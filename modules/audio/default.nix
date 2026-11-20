{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.hardware.asus-zenbook-a16.audio;
  sp = cfg.speakers;
  inherit (lib) mkOption types;

  ucm = pkgs.callPackage ./ucm.nix {
    inherit (cfg) longName compressor;
    levels = {
      inherit (cfg.levels) wooferDigital tweeterDigital ampVolume;
    };
  };
  ucm2 = "${ucm}/share/alsa/ucm2";

  graph = import ./graph.nix { inherit lib cfg; };

  position = types.enum [
    "FL"
    "FR"
    "RL"
    "RR"
  ];

  matchCard = extra: [
    ({ "alsa.long_card_name" = cfg.longName; } // extra)
    ({ "api.alsa.card.longname" = cfg.longName; } // extra)
  ];

  waitForCard = pkgs.writeShellScript "zenbook-a16-audio-wait" ''
    for _ in $(${pkgs.coreutils}/bin/seq ${toString cfg.cardWaitSeconds}); do
      if ${pkgs.gnugrep}/bin/grep -qsF -- ${lib.escapeShellArg cfg.longName} /proc/asound/cards; then
        exit 0
      fi
      ${pkgs.coreutils}/bin/sleep 1
    done
    echo "zenbook-a16-audio: no ${cfg.longName} card after ${toString cfg.cardWaitSeconds} s; starting anyway" >&2
  '';
in
{
  options.hardware.asus-zenbook-a16.audio = {
    enable = lib.mkEnableOption ''
      the Zenbook A16's speakers: UCM profile, PipeWire/WirePlumber, and a
      stereo crossover sink in front of the four amps'';

    longName = mkOption {
      type = types.str;
      default = "ASUSTeKCOMPUTERINC.-ZenbookA16UX3607OA-1.0-UX3607OA";
      description = "The card's ALSA long name (/proc/asound/cards); selects the UCM profile and the WirePlumber rules.";
    };

    levels = {
      wooferDigital = mkOption {
        type = types.ints.between 0 81;
        default = 81;
        description = ''
          "WSA/WSA2 WSA_RX0 Digital Volume" (woofer paths), set by the UCM
          Speaker device. 84 = 0 dB, 1 dB steps; snd-x1e80100 caps it at 81
          (-3 dB, the boot value). Default 81 = -3 dB, from the reference
          measurement (2026-10-25): with PA 6, compressors off and the
          filter at 0 dB this is the most clean gain there is, and still
          below the reference level.
        '';
      };
      tweeterDigital = mkOption {
        type = types.ints.between 0 81;
        default = 78;
        description = ''
          "WSA/WSA2 WSA_RX1 Digital Volume" (tweeter paths). Default 78 =
          -6 dB, 3 dB below the woofers: measured 2026-10-25 against the
          reference, 3.15-16 kHz then within a few dB of it
          (FixItFoundry runs 81/77, i.e. 4 dB).
        '';
      };
      ampVolume = mkOption {
        type = types.ints.between 0 6;
        default = 6;
        description = ''
          WSA884x "PA Volume" on all four amps: 0..6 = -9..0 dB in 1.5 dB
          steps (6 is this driver's maximum; upstream UCM's 12 is rejected).
        '';
      };
    };

    compressor = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Both compressor stages, set by the UCM Speaker device: the WSA/WSA2
        macros' "WSA_COMP1/2 Switch" and the amps' "<Amp> COMP Switch".
        Upstream's UCM and FixItFoundry turn them on; off here because in a
        listening test (2026-10-22) they made the bassline inaudible and
        put a burst (attack) at the start of playback.
      '';
    };

    keepActive = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Never suspend (close) the speaker PCM when idle
        (session.suspend-timeout-seconds = 0), as FixItFoundry's WirePlumber
        policy does, so the UCM route and the amps stay set up across idle
        periods instead of being torn down and re-enabled. Costs the amps'
        idle power while PipeWire runs.
      '';
    };

    cardWaitSeconds = mkOption {
      type = types.ints.unsigned;
      default = 20;
      description = ''
        WirePlumber (user service) waits up to this long for the card before
        starting, then starts regardless. With the DSPs started in the initrd
        the card is registered before switch-root, so this normally returns
        at once. 0 disables the wait.
      '';
    };

    rawNodeName = mkOption {
      type = types.str;
      default = "alsa_output.zenbook-a16-speaker-amps";
      description = "node.name given to the raw 4-channel UCM speaker sink.";
    };

    speakers = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          The stereo crossover sink. Off: applications get the raw 4-channel
          sink (FL FR RL RR, unfiltered) instead.
        '';
      };
      nodeName = mkOption {
        type = types.str;
        default = "zenbook-a16-speakers";
      };
      description = mkOption {
        type = types.str;
        default = "Laptop Speakers";
      };
      priority = mkOption {
        type = types.int;
        default = 1500;
        description = "priority.session of the stereo sink (the raw sink gets 100).";
      };
      hideRaw = mkOption {
        type = types.bool;
        default = true;
        description = "Hide the raw 4-channel sink from clients (WirePlumber hide-parent).";
      };
      crossoverHz = mkOption {
        type = types.numbers.between 200 12000;
        default = 3000;
        description = ''
          Crossover frequency (LR4): the tweeters' high-pass, and the
          woofers' low-pass unless wooferLowpassHz is set. 3000 measured
          2026-10-25 (README "A16 audio").
        '';
      };
      wooferLowpassHz = mkOption {
        type = types.nullOr (types.numbers.between 200 12000);
        default = 3400;
        description = ''
          The woofers' LR4 low-pass, if not at crossoverHz (null). Default
          3400 ("candidate 2" of the 2026-10-25 measurement): the overlap
          fills the 2-3 kHz dip by ~2 dB.
        '';
      };
      wooferHighpassHz = mkOption {
        type = types.nullOr (types.numbers.between 20 1000);
        default = 100;
        description = ''
          2nd-order Butterworth high-pass on the woofers, so content they
          can't reproduce doesn't cost excursion (no speaker protection runs
          here). null: none. A guess; the 2026-10-25 measurement matched
          the reference from 200 Hz up with it in place.
        '';
      };
      highShelf = mkOption {
        type = types.nullOr (
          types.submodule {
            options = {
              freq = mkOption { type = types.numbers.between 1000 20000; };
              q = mkOption { type = types.numbers.between 0.1 4; };
              gainDb = mkOption { type = types.numbers.between (-12) 0; };
            };
          }
        );
        default = {
          freq = 11500;
          q = 0.707;
          gainDb = -3;
        };
        description = ''
          High shelf (bq_highshelf) on both input channels, ahead of the
          crossover. null: none. Default 11.5 kHz, Q 0.707, -3 dB
          ("candidate 2", 2026-10-25): 10-16 kHz was 2.5-4 dB above the reference.
        '';
      };
      gainDb = mkOption {
        type = types.numbers.between (-60) 0;
        default = 0;
        description = ''
          Fixed gain on all four outputs, in dB, ahead of the limiter. 0 dB
          is the level matched to the reference on 2026-10-25 (with levels.* 81/78).
          The crossover raises music peaks by ~3.5 dB, so without the limiter
          full-scale content clips at the raw sink.
        '';
      };
      limiter = {
        enable = mkOption {
          type = types.bool;
          default = true;
          description = ''
            Lookahead limiter (swh fast_lookahead_limiter, LADSPA) per pair
            of outputs, last in the graph. It keeps the 4-channel output
            under ceilingDb so nothing clips digitally at the raw sink (the
            crossover's phase shifts raise peaks above the input's, and the
            desktop volume can go past 100 %). It is not speaker protection:
            Linux has no Qualcomm speaker protection (no VISENSE feedback,
            no excursion or temperature model); a full-scale signal still
            reaches the amps at the UCM levels.
          '';
        };
        ceilingDb = mkOption {
          type = types.numbers.between (-20) 0;
          default = -1;
          description = "Limiter ceiling (\"Limit (dB)\"), dBFS.";
        };
        releaseSeconds = mkOption {
          type = types.numbers.between 0.01 2;
          default = 0.08;
          description = "Limiter release time (\"Release time (s)\").";
        };
        package = lib.mkPackageOption pkgs "ladspaPlugins" { };
      };
      dsp = {
        enable = mkOption {
          type = types.bool;
          default = false;
          description = ''
            Run the speaker processing (high shelf, LR4 crossover, woofer
            high-pass, per-pair gains, limiter: the options above, same
            values) on the ADSP instead of in a PipeWire filter-chain:
            an A16 topology with MFC (2 -> 4), MSIIR and SAL modules on
            WSA_CODEC_DMA_RX_0, configured by cfg blobs computed from these
            options (pkgs/a16-firmware/topology/speaker-dsp-blobs.py). Needs
            the a16 kernel's audioreach cfg-blob patches
            (hosts/zenbook-a16/patches/audio-dsp). "Laptop Speakers" stays,
            as a plain stereo sink. The tweeters only ever get the DSP's
            high-passed feed: the device graph drops everything but the
            stream's FL/FR, and a rejected cfg blob fails the stream.
            Differences from the filter-chain: the limiter (SAL, a
            zero-crossing limiter per channel) has its own attack/release
            (releaseSeconds is not used), and nothing can be changed live
            (a change needs a rebuild and a reboot).
          '';
        };
        params = mkOption {
          type = types.attrs;
          readOnly = true;
          internal = true;
          description = "The tuning as pkgs/a16-firmware's speakerDsp takes it (from the options above).";
          default = {
            sampleRate = 48000;
            inherit (sp)
              highShelf
              crossoverHz
              wooferLowpassHz
              wooferHighpassHz
              gainDb
              wooferGainDb
              tweeterGainDb
              ;
            limiter = {
              inherit (sp.limiter) enable ceilingDb;
            };
          };
        };
      };
      wooferGainDb = mkOption {
        type = types.numbers.between (-30) 6;
        default = 0;
        description = "Trim of the woofer pair relative to gainDb (gainDb + trim must stay <= 0).";
      };
      tweeterGainDb = mkOption {
        type = types.numbers.between (-30) 6;
        default = 0;
        description = "Trim of the tweeter pair relative to gainDb (gainDb + trim must stay <= 0).";
      };
      channels = mkOption {
        type = types.submodule {
          options = {
            wooferLeft = mkOption {
              type = position;
              default = "FL";
            };
            wooferRight = mkOption {
              type = position;
              default = "FR";
            };
            tweeterLeft = mkOption {
              type = position;
              default = "RL";
            };
            tweeterRight = mkOption {
              type = position;
              default = "RR";
            };
          };
        };
        default = { };
        description = ''
          Which channel of the 4-channel PCM reaches which speaker. Default
          from the kernel (sound/soc/qcom/x1e80100.c maps the backend's
          slots FL LB FR RB; q6apm labels the PCM FL FR LS RS; slots 0/1 go
          to the WSA macro, 2/3 to WSA2; DT routes RX0 -> WSA_SPK1 -> Woofer*,
          RX1 -> WSA_SPK2 -> Tweeter*): FL = WooferLeft, FR = WooferRight,
          RL = TweeterLeft, RR = TweeterRight, which is also FixItFoundry's
          changelog. Confirmed by listening 2026-10-22 (README "A16 audio").
        '';
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          let
            c = sp.channels;
          in
          lib.length (
            lib.unique [
              c.wooferLeft
              c.wooferRight
              c.tweeterLeft
              c.tweeterRight
            ]
          ) == 4;
        message = "hardware.asus-zenbook-a16.audio.speakers.channels must use FL, FR, RL and RR once each.";
      }
      {
        assertion = sp.gainDb + lib.max sp.wooferGainDb sp.tweeterGainDb <= 0;
        message = "hardware.asus-zenbook-a16.audio.speakers: gainDb plus a pair's trim must not exceed 0 dB.";
      }
      {
        assertion = sp.wooferHighpassHz == null || sp.wooferHighpassHz < sp.crossoverHz / 2;
        message = "hardware.asus-zenbook-a16.audio.speakers.wooferHighpassHz must be well below crossoverHz.";
      }
    ];

    security.rtkit.enable = lib.mkDefault true;
    services.pipewire = {
      enable = lib.mkDefault true;
      alsa.enable = lib.mkDefault true;
      pulse.enable = lib.mkDefault true;
      wireplumber.enable = true;

      wireplumber.extraConfig."60-zenbook-a16-speakers" = {
        "monitor.alsa.rules" = [
          {
            matches = matchCard { "device.name" = "~alsa_card.*"; };
            actions.update-props = {
              "device.routes.default-sink-volume" = 1.0;
            };
          }
          {
            matches = matchCard { "node.name" = "~alsa_output.*"; };
            actions.update-props = {
              "node.name" = cfg.rawNodeName;
              "node.description" = "Zenbook A16 speaker amps (raw, 4 ch)";
              "audio.channels" = 4;
              "audio.position" = [
                "FL"
                "FR"
                "RL"
                "RR"
              ];
              "channelmix.upmix" = false;
              "priority.session" = 100;
            }
            // lib.optionalAttrs cfg.keepActive { "session.suspend-timeout-seconds" = 0; };
          }
        ];
      }
      // lib.optionalAttrs sp.enable {
        "node.software-dsp.rules" = [
          {
            matches = [ { "node.name" = cfg.rawNodeName; } ];
            actions.create-filter = {
              filter-graph = graph;
              hide-parent = sp.hideRaw;
            };
          }
        ];
        "wireplumber.profiles".main = {
          "node.software-dsp" = "required";
          "pw.node-factory.adapter" = "required";
        };
      };
    };

    environment.variables.ALSA_CONFIG_UCM2 = ucm2;
    systemd.user.services.pipewire.environment.ALSA_CONFIG_UCM2 = ucm2;
    systemd.user.services.wireplumber.environment.ALSA_CONFIG_UCM2 = ucm2;
    systemd.user.services.wireplumber.serviceConfig.ExecStartPre = lib.mkIf (cfg.cardWaitSeconds > 0) [
      waitForCard
    ];

    environment.systemPackages = [ pkgs.alsa-utils ];
  };
}
