{
  config,
  lib,
  options,
  pkgs,
  zenbook-a16-linux,
  ...
}:

# The A16 kernel: a mainline release (or -rc) plus the patch queue in
# kernel/<variant>.json, built from the series' own .config (FixItFoundry's,
# from the zenbook-a16-linux input) with `kernel.config` appended. Every
# `kernel.config` entry is checked after `make oldconfig`: a symbol Kconfig
# drops fails the build instead of silently reverting.
let
  cfg = config.hardware.asus-zenbook-a16.kernel;
  kpkgs = cfg.pkgs;

  variants =
    lib.mapAttrs'
      (
        file: _:
        lib.nameValuePair (lib.removeSuffix ".json" file) (lib.importJSON (../kernel/variants + "/${file}"))
      )
      (
        lib.filterAttrs (n: t: t == "regular" && lib.hasSuffix ".json" n) (
          builtins.readDir ../kernel/variants
        )
      );
  series = variants.${cfg.variant};

  # Per-patch metadata (file or input path, author, licence, source,
  # status): kernel/manifest.json; kernel/check.nix enforces its policy.
  manifest = lib.importJSON ../kernel/manifest.json;
  # Optional debug/test patches (kernel/lab-patches.json): applied in their
  # place in the queue only when kernel.labPatches.<name>.enable is set.
  labPatches = lib.importJSON ../kernel/lab-patches.json;
  wanted =
    name:
    let
      opt = manifest.${name}.labOption or null;
    in
    opt == null || cfg.labPatches.${opt}.enable;
  patchOf =
    name:
    let
      p = manifest.${name};
    in
    {
      inherit name;
      patch = if p ? input then "${zenbook-a16-linux}/${p.input}" else ../kernel/patches + "/${p.file}";
    };

  # ccache for the kernel build. The cache dir must be visible in the build
  # sandbox (nix.settings.extra-sandbox-paths on the builder) and writable by
  # the build users; elsewhere the wrapper bypasses ccache, so the same
  # derivation still builds.
  kernelStdenv =
    if cfg.ccache.enable then
      kpkgs.ccacheStdenv.override {
        inherit (kpkgs) stdenv;
        extraConfig = ''
          export CCACHE_DIR=${lib.escapeShellArg cfg.ccache.dir}
          if [ -d "$CCACHE_DIR" ] && [ -w "$CCACHE_DIR" ]; then
            export CCACHE_COMPRESS=1
            # The kernel regenerates autoconf.h etc. every build; compare
            # contents, not timestamps.
            export CCACHE_SLOPPINESS=include_file_mtime,include_file_ctime,time_macros
          else
            export CCACHE_DISABLE=1
          fi
        '';
      }
    else
      kpkgs.stdenv;

  readConfig =
    file:
    lib.listToAttrs (
      lib.concatMap (
        line:
        let
          m = lib.match "(CONFIG_[^=]+)=([ym])" line;
        in
        lib.optional (m != null) (lib.nameValuePair (lib.elemAt m 0) (lib.elemAt m 1))
      ) (lib.splitString "\n" (builtins.readFile file))
    );

  mkA16Kernel =
    {
      name,
      configFile,
      kernelPatches,
      version,
      src,
      dbg,
    }:
    let
      line = n: v: if v == "n" then "# CONFIG_${n} is not set" else "CONFIG_${n}=${v}";
      expected = lib.mapAttrsToList line dbg;
      configfile = kpkgs.writeText "${name}.config" ''
        ${builtins.readFile configFile}
        # --- snap2xee lab.zenbook.kernelConfig ---
        CONFIG_LOCALVERSION=""
        ${lib.concatLines expected}
      '';
      kernelConfig =
        lib.filterAttrs (_: v: v != "n") (readConfig configFile)
        // lib.mapAttrs' (n: v: lib.nameValuePair "CONFIG_${n}" v) (lib.filterAttrs (_: v: v != "n") dbg);
    in
    (kpkgs.linuxManualConfig {
      inherit
        version
        src
        configfile
        kernelPatches
        ;
      modDirVersion = version;
      stdenv = kernelStdenv;
      features.efiBootStub = kernelConfig.CONFIG_EFI_STUB or null == "y";
      config = lib.removeAttrs kernelConfig (
        lib.mapAttrsToList (n: _: "CONFIG_${n}") (lib.filterAttrs (_: v: v == "n") dbg)
      );
    }).overrideAttrs
      (old: {
        preUnpack = "";
        postInstall = ''
          mkdir -p "$dev/lib/modules/${version}"
          cp -a -- "$NIX_BUILD_TOP/source" "$dev/lib/modules/${version}/source"
          cd "$dev/lib/modules/${version}/source"
        ''
        + old.postInstall;

        postConfigure = (old.postConfigure or "") + ''
          bad=0
          while IFS= read -r want; do
            [ -n "$want" ] || continue
            if ! grep -qxF -- "$want" "$buildRoot/.config"; then
              echo "lab.zenbook.kernelConfig: '$want' did not survive oldconfig" >&2
              bad=1
            fi
          done <<'EOF'
          ${lib.concatLines expected}
          EOF
          [ "$bad" = 0 ]
        '';
      });

  kernel = mkA16Kernel {
    name = cfg.variant;
    inherit (series) version;
    src = kpkgs.fetchzip { inherit (series.src) url hash; };
    configFile = "${zenbook-a16-linux}/${series.configFile}";
    kernelPatches = map patchOf (lib.filter wanted series.patches) ++ cfg.extraPatches;
    dbg = cfg.config;
  };
in
{
  options.hardware.asus-zenbook-a16.kernel = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Boot the A16 kernel built here (boot.kernelPackages).";
    };
    variant = lib.mkOption {
      type = lib.types.enum (lib.attrNames variants);
      default = "a16-7.4rc3-jack";
      description = "Which patch queue in kernel/variants/ to build.";
    };
    config = lib.mkOption {
      type = lib.types.attrsOf (
        lib.types.enum [
          "y"
          "m"
          "n"
        ]
      );
      default = { };
      description = ''
        Kconfig overrides (symbol without CONFIG_) appended to the series
        .config. The board's own values are set by this module; add yours
        (debug options, consoles) here. Each is checked after oldconfig.
      '';
    };
    # Debug/test patches from the bring-up, off by default; see README "Lab
    # patches" and kernel/lab-patches.json.
    labPatches = lib.mapAttrs (_: p: {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = p.description + " (Patch ${p.patch}; off by default.)";
      };
    }) labPatches;
    extraPatches = lib.mkOption {
      type = lib.types.listOf lib.types.attrs;
      default = [ ];
      description = "Further kernelPatches ({ name; patch; }) applied after the queue.";
    };
    pkgs = lib.mkOption {
      type = lib.types.raw;
      default = pkgs;
      defaultText = lib.literalExpression "pkgs";
      description = ''
        Package set the kernel is built with. Set it to a pinned nixpkgs
        (e.g. this flake's) so bumping the system's nixpkgs doesn't rebuild
        the kernel.
      '';
    };
    ccache = {
      enable = lib.mkEnableOption "ccache for the kernel build (needs the cache dir in the builder's sandbox)";
      dir = lib.mkOption {
        type = lib.types.str;
        default = "/var/cache/ccache";
        description = "ccache directory on the builder.";
      };
    };
    packages = lib.mkOption {
      type = lib.types.raw;
      readOnly = true;
      default = kpkgs.linuxPackagesFor kernel;
      defaultText = lib.literalMD "the A16 kernel's package set";
      description = "linuxPackages of the A16 kernel.";
    };
  };

  config = lib.mkMerge [
    { hardware.asus-zenbook-a16.kernel.config = lib.mapAttrs (_: lib.mkDefault) series.config; }
    (lib.mkIf cfg.enable {
      boot.kernelPackages = cfg.packages;
      hardware.asus-zenbook-a16.firmware.dsp.extraInitrdModules = series.initrdModules;
    })
    # With kernel.pkgs pinned, the kernel and its external modules
    # (boot.kernelPackages.*) come from that package set, so combine them
    # (system.modulesTree) with its aggregateModules too. A newer system
    # nixpkgs (2026-11, kmodigest signing) expects modules.cer and a .ko.cer
    # per external module, which an older package set's builds don't have.
    (lib.mkIf (cfg.enable && options.hardware.asus-zenbook-a16.kernel.pkgs.highestPrio < 1500) {
      nixpkgs.overlays = [ (_: _: { inherit (kpkgs) aggregateModules; }) ];
    })
  ];
}
