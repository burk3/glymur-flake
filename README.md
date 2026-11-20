# glymur-flake

NixOS hardware support for the ASUS Zenbook A16 (UX3607OA, Snapdragon X2
Elite / "Glymur"): a patched kernel, firmware packaging, device tree and
command line, speakers (UCM, PipeWire, DSP topology), and a few board
workarounds, as one NixOS module.

> Status: pre-release skeleton, not yet published. Option names may change.

## Use

```nix
{
  inputs.glymur-flake.url = "<this flake>";
  outputs = { nixpkgs, glymur-flake, ... }: {
    nixosConfigurations.my-a16 = nixpkgs.lib.nixosSystem {
      modules = [
        glymur-flake.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
```

Importing the module means the system runs on an A16 (nixos-hardware style).
Options live under `hardware.asus-zenbook-a16`:

| Option | Default | |
|---|---|---|
| `kernel.enable` | true | boot the A16 kernel |
| `kernel.variant` | newest queue | patch queue in `kernel/` |
| `kernel.config` | board values | extra Kconfig symbols (checked after oldconfig) |
| `kernel.extraPatches` | `[ ]` | patches after the queue |
| `kernel.pkgs` | `pkgs` | package set the kernel is built with (pin it to avoid rebuilds) |
| `kernel.ccache.{enable,dir}` | off | ccache in the build sandbox |
| `firmware.*` | on | DSP, Wi-Fi board data, Bluetooth; DSPs start in the initrd |
| `audio.*` | on | speakers |
| `wifi.unloadAcrossSuspend` | true | ath12k resume workaround |
| `edpRetrain.enable` | true | eDP link-training recovery |
| `irEmitter.stage`, `howdy.*` | off | IR camera face login |
| `usbWakeup.disabledControllers` | all three | USB wakeup off for s2idle |

## Firmware

OEM firmware is never stored in this repository. It is fetched from ASUS's
public support download at build time (fixed hash), or must be added to the
local Nix store by the user.

## Lab tooling: how this laptop was brought up

The A16 is a production laptop with fused (secure-boot-locked) firmware: no
JTAG, no debug UART, a firmware that scrubs RAM on every reset and ignores
boot-order changes from Linux. The lab outputs are the tools that made
bring-up possible anyway, published so others can learn from (or reuse)
the approach:

- `nixosModules.lab`: the device-side harness. Kernel console and login on
  an FTDI USB null-modem cable, netconsole with a late configfs fallback and
  log backlog, a 1 Hz heartbeat (a silent box vs. a reset), the hardware
  watchdog, systemd-boot boot counting with a delayed blessing so a bad
  generation falls back on its own, telemetry and log shipping, diagnostic
  snapshots (`a16-snap`), milestone benchmarks.
- `nixosModules.observer-host`: the other end, on a workstation. A
  container that owns the serial cable, receives heartbeats and netconsole,
  scrapes metrics, and the `a16` CLI (status, logs, boots, red-flag kernel
  log signatures, snapshots and diffs).
- `packages.*.lab-lease`: advisory leases so several agents and a human can
  share the laptop, a builder and a kernel checkout without colliding.

Addresses, MACs, serial numbers and users are options without defaults:
set them in your own configuration.

Power-button presses during unattended reboot tests came from a Fingerbot
(GL.iNet FGB-01) on the power key, driven over Bluetooth from the
workstation; the firmware's own watchdog/panic resets were preferred.

### Lab patches (kernel, off by default)

Debug and test patches from the bring-up, kept for whoever brings up the
next Glymur laptop (or debugs this one). Each is a
`hardware.asus-zenbook-a16.kernel.labPatches.<name>.enable` option
(descriptions in `kernel/lab-patches.json`), applied in its place in the
queue when enabled; manifest status `lab`:

| Option | What it does | Use it when |
|---|---|---|
| `dpLinkTrainingFaultInjection` | `msm.dp_lt_inject_eq_fail=N` fails the next N DP/eDP link trainings on purpose | testing link-training retry/fallback, error reporting, or a recovery service, without waiting for a rare real failure |
| `qmpComboDpPowerOnDiag` | logs the QMP combo PHY status registers on a DP power-on timeout | external DP over USB-C fails with -110 (PHY power-on); checking register offsets on a new SoC/PHY |

One more lab patch is always applied: FixItFoundry's
`0010-LOCAL-EXPERIMENT-preserve-test4-SoundWire-compatibil` (their
SoundWire experiment bundle); FixItFoundry's later patches are built on
it, so it can't be turned off without regenerating theirs.

## Licence

MIT (LICENSE) for the Nix code, scripts and tools. Kernel patches and device
tree overlays are GPL-2.0-only, with the authorship recorded in each file
(REUSE.toml).
