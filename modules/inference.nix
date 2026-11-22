{
  config,
  lib,
  pkgs,
  ...
}:

# A local model server on the CDSP (Hexagon NPU), off by default.
# llama-server's native router supplies model selection, a web UI and the
# OpenAI API. Keep its service implementation in nixpkgs; this module adds
# model presets and the A16's non-secure CDSP access, without restarting DSPs
# (they start in the initrd, firmware.nix). No models by default: the system
# declares its own catalog (`models`), e.g. GGUFs pinned with fetchurl.
let
  cfg = config.hardware.asus-zenbook-a16.inference;
  inherit (lib)
    mkEnableOption
    mkIf
    mkOption
    types
    ;
  valueType = types.oneOf [
    types.str
    types.int
    types.float
    types.bool
    types.path
  ];
  settingsType = types.attrsOf valueType;
  presets = {
    "*" = cfg.modelDefaults;
  }
  // lib.mapAttrs (
    _: model:
    model.settings
    // lib.optionalAttrs (model.path != null) { model = toString model.path; }
    // lib.optionalAttrs (model.hfRepo != null) { hf-repo = model.hfRepo; }
    // lib.optionalAttrs (model.hfFile != null) { hf-file = model.hfFile; }
  ) cfg.models;
  presetsFile = pkgs.writeText "a16-inference-models.ini" (
    "version = 1\n\n" + lib.generators.toINI { } presets
  );
in
{
  options.hardware.asus-zenbook-a16.inference = {
    enable = mkEnableOption "the A16 model server with Hexagon acceleration";
    package = mkOption {
      type = types.package;
      default = pkgs.callPackage ../pkgs/llama-cpp-hexagon { };
      defaultText = lib.literalExpression "pkgs.callPackage ../pkgs/llama-cpp-hexagon { }";
      description = "llama.cpp package providing llama-server and the Hexagon runtime.";
    };
    host = mkOption {
      type = types.str;
      default = "127.0.0.1";
      description = "Listen address for the web UI and OpenAI-compatible API.";
    };
    port = mkOption {
      type = types.port;
      default = 8080;
    };
    openFirewall = mkOption {
      type = types.bool;
      default = false;
    };
    apiKeyFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "/run/secrets/inference-api-key";
      description = ''
        Absolute path to an API key file, passed with systemd credentials.
        Use a runtime path string so the secret is not copied into the Nix store.
        null leaves the API unauthenticated (loopback by default).
      '';
    };
    maxLoadedModels = mkOption {
      type = types.ints.positive;
      default = 1;
      description = "Maximum resident model instances; the router evicts older models as needed.";
    };
    modelDefaults = mkOption {
      type = settingsType;
      default = { };
      description = ''
        Native llama-server preset options shared by all models. Per-model
        settings override these. Use sleep-idle-seconds = -1 to disable idle
        unloading. Put inference options here, rather than on the router CLI,
        because CLI options override individual model presets.
      '';
    };
    models = mkOption {
      default = { };
      description = ''
        Named model presets. Attribute names become API model IDs and web UI
        choices. Provide either a local GGUF path (including a fetchurl store
        path) or a Hugging Face repository. HF models download on first use
        into /var/cache/llama-cpp. An empty set allows adding models in the UI.
      '';
      type = types.attrsOf (
        types.submodule {
          options = {
            path = mkOption {
              type = types.nullOr (types.either types.path types.str);
              default = null;
              description = "Absolute path to a readable GGUF file; use a string for files outside the Nix store.";
            };
            hfRepo = mkOption {
              type = types.nullOr types.str;
              default = null;
              example = "bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_0";
              description = "Hugging Face repository, optionally with a quantization tag. Runtime downloads are not content-pinned.";
            };
            hfFile = mkOption {
              type = types.nullOr types.str;
              default = null;
              description = "Optional exact GGUF filename within hfRepo.";
            };
            settings = mkOption {
              type = settingsType;
              default = { };
              example = {
                ctx-size = 8192;
                temp = 0.6;
              };
              description = "Native llama-server preset options, without leading dashes.";
            };
          };
        }
      );
    };
    hexagon = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Grant the service non-secure CDSP and system DMA-heap access and
          select HTP0 by default. Disable when supplying a CPU package for
          testing; this does not change the kernel or start/restart DSPs.
        '';
      };
      powerMode = mkOption {
        type = types.enum [
          "low_power_saver"
          "power_saver"
          "high_power_saver"
          "low_balanced"
          "balanced"
          "high_performance"
          "sustained_high_performance"
          "burst"
        ];
        default = "balanced";
        description = "Hexagon runtime performance request; balanced keeps dynamic clock scaling enabled.";
      };
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = !cfg.hexagon.enable || pkgs.stdenv.hostPlatform.isAarch64;
        message = "hardware.asus-zenbook-a16.inference.hexagon requires an aarch64 host; disable it and override package for CPU tests.";
      }
      {
        assertion = cfg.apiKeyFile == null || lib.hasPrefix "/" cfg.apiKeyFile;
        message = "hardware.asus-zenbook-a16.inference.apiKeyFile must be an absolute runtime path.";
      }
    ]
    ++ lib.mapAttrsToList (name: model: {
      assertion =
        name != "*"
        && !lib.hasInfix "[" name
        && !lib.hasInfix "]" name
        && !lib.hasInfix "\n" name
        && !lib.hasInfix "\r" name
        && ((model.path != null) != (model.hfRepo != null))
        && (model.path == null || lib.hasPrefix "/" (toString model.path))
        && (model.hfFile == null || model.hfRepo != null)
        && !(model.settings ? model)
        && !(model.settings ? hf-repo)
        && !(model.settings ? hf-file);
      message = "hardware.asus-zenbook-a16.inference.models.${name}: use a valid INI section name and exactly one of absolute path or hfRepo; hfFile requires hfRepo. Set model sources through those options, not settings.";
    }) cfg.models;

    hardware.asus-zenbook-a16.inference.modelDefaults = {
      ctx-size = lib.mkDefault 4096;
      parallel = lib.mkDefault 1;
      sleep-idle-seconds = lib.mkDefault 300;
      load-on-startup = lib.mkDefault false;
      dedup-cache-models = lib.mkDefault true;
    }
    // lib.optionalAttrs cfg.hexagon.enable {
      device = lib.mkDefault "HTP0";
      n-gpu-layers = lib.mkDefault 99;
    };

    services.llama-cpp = {
      enable = true;
      inherit (cfg) package openFirewall;
      settings = {
        inherit (cfg) host port;
        models-preset = presetsFile;
        models-max = cfg.maxLoadedModels;
      }
      // lib.optionalAttrs (cfg.apiKeyFile != null) {
        api-key-file = "%d/api-key";
      };
    };

    users.groups.a16-inference = mkIf cfg.hexagon.enable { };
    services.udev.extraRules = lib.optionalString cfg.hexagon.enable ''
      SUBSYSTEM=="misc", KERNEL=="fastrpc-cdsp", GROUP="a16-inference", MODE="0660"
      SUBSYSTEM=="dma_heap", KERNEL=="system", GROUP="a16-inference", MODE="0660"
    '';
    systemd.services.llama-cpp = {
      environment = lib.optionalAttrs cfg.hexagon.enable {
        GGML_HEXAGON_POWER_MODE = cfg.hexagon.powerMode;
      };
      serviceConfig = lib.mkMerge [
        (mkIf (cfg.apiKeyFile != null) {
          LoadCredential = [ "api-key:${cfg.apiKeyFile}" ];
        })
        (mkIf cfg.hexagon.enable {
          SupplementaryGroups = [ "a16-inference" ];
          # User namespaces do not map the supplementary hardware group.
          # Retain DynamicUser and the rest of nixpkgs' service sandbox.
          PrivateUsers = lib.mkForce false;
          DevicePolicy = "closed";
          DeviceAllow = [
            "/dev/fastrpc-cdsp rw"
            "/dev/dma_heap/system rw"
          ];
        })
      ];
    };
  };
}
