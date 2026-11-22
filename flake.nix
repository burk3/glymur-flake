{
  description = "NixOS hardware support for the ASUS Zenbook A16 (UX3607OA, Snapdragon X2 Elite / Glymur)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # FixItFoundry's A16 patch queues and kernel .config (patches are
    # referenced from it, not copied).
    zenbook-a16-linux = {
      url = "github:FixItFoundry/zenbook-a16-linux";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      zenbook-a16-linux,
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
      aarch64 = nixpkgs.legacyPackages.aarch64-linux;

      patchPolicy = import ./kernel/check.nix { inherit lib; };

      # The patch queue for other distributions: patches in order (numbered),
      # series, manifest.json and the licence texts. No Nix needed to use it.
      kernelPatches =
        pkgs: variant:
        let
          series = lib.importJSON (./kernel/variants + "/${variant}.json");
          m = patchPolicy.manifest;
          entries = lib.imap1 (i: name: {
            inherit name;
            n = lib.fixedWidthNumber 4 i;
            src =
              if m.${name} ? input then
                "${zenbook-a16-linux}/${m.${name}.input}"
              else
                ./kernel/patches + "/${m.${name}.file}";
          }) series.patches;
          manifestJson = builtins.toJSON {
            inherit variant;
            inherit (series)
              version
              src
              configFile
              config
              ;
            patches = map (
              e:
              m.${e.name}
              // {
                inherit (e) name;
                file = "${e.n}-${e.name}.patch";
              }
            ) entries;
          };
        in
        pkgs.runCommand "a16-kernel-patches-${variant}"
          {
            passAsFile = [ "manifestJson" ];
            inherit manifestJson;
          }
          (
            ''
              mkdir -p $out/patches $out/LICENSES
              cp ${./LICENSES}/* $out/LICENSES/
              cp ${./kernel/README.md} $out/README.md
              cp "$manifestJsonPath" $out/manifest.json
            ''
            + lib.concatMapStrings (e: ''
              cp ${e.src} $out/patches/${e.n}-${e.name}.patch
              echo ${e.n}-${e.name}.patch >> $out/patches/series
            '') entries
          );

      # A minimal A16 system, for evaluation checks and `packages.kernel`.
      example = lib.nixosSystem {
        modules = [
          self.nixosModules.default
          {
            boot.loader.systemd-boot.enable = true;
            fileSystems."/" = {
              device = "/dev/disk/by-label/nixos";
              fsType = "ext4";
            };
            system.stateVersion = "26.05";
          }
        ];
      };
    in
    {
      nixosModules = {
        asus-zenbook-a16 = {
          imports = [ ./modules ];
          _module.args.zenbook-a16-linux = zenbook-a16-linux;
        };
        default = self.nixosModules.asus-zenbook-a16;
        # The A16 test harness (serial/netconsole, heartbeat, boot counting,
        # observability, a16-snap, bench): import next to the hardware
        # module; lab.network.* and lab.zenbook.serialConsole.* are yours.
        lab = ./modules/lab;
        # The observer host side: the a16-obs container (serial owner,
        # heartbeat/netconsole sinks, metrics) and the a16 CLI.
        observer-host = ./modules/observer-host.nix;
      };

      # The nixpkgs this flake is tested with; pass it as
      # hardware.asus-zenbook-a16.kernel.pkgs to decouple kernel rebuilds
      # from the system's nixpkgs.
      lib.kernelPkgs = aarch64;

      packages = lib.recursiveUpdate (forAllSystems (system: {
        a16-observer = nixpkgs.legacyPackages.${system}.callPackage ./pkgs/a16-observer { };
        a16-bench = nixpkgs.legacyPackages.${system}.callPackage ./pkgs/a16-bench { };
        # Advisory leases for agents sharing the laptop, a builder and a
        # kernel checkout (LAB_LEASE_RESOURCES or leaseResources names them).
        lab-lease = nixpkgs.legacyPackages.${system}.callPackage ./pkgs/lab-lease { };
        a16 = self.packages.${system}.a16-observer;
      })) {
      aarch64-linux = {
        kernel = example.config.boot.kernelPackages.kernel;
        hexagon-fastrpc = aarch64.callPackage ./pkgs/hexagon-fastrpc { };
        hexagon-dsp-libs = aarch64.callPackage ./pkgs/hexagon-dsp-libs { };
        a16-ir-bridge = aarch64.callPackage ./pkgs/a16-ir-bridge { };
      };

      # Architecture-independent: the queue as files (default variant).
      x86_64-linux.kernel-patches = kernelPatches nixpkgs.legacyPackages.x86_64-linux example.config.hardware.asus-zenbook-a16.kernel.variant;
      };

      nixosConfigurations.example = example;

      # Evaluation only: instantiates the example system (no kernel build).
      checks = forAllSystems (system: {
        # kernel/check.nix: manifest complete, NOT-FOR-UPSTREAM marks, no
        # sign-off added on someone's behalf.
        patch-manifest =
          if patchPolicy.errors == [ ] then
            nixpkgs.legacyPackages.${system}.writeText "patch-manifest" (builtins.toJSON patchPolicy.summary)
          else
            throw ("patch policy:\n" + lib.concatLines patchPolicy.errors);
        example-eval = nixpkgs.legacyPackages.${system}.writeText "example-toplevel" (
          builtins.unsafeDiscardStringContext example.config.system.build.toplevel.drvPath
        );
      });

      formatter = forAllSystems (system: nixpkgs.legacyPackages.${system}.nixfmt-tree);
    };
}
