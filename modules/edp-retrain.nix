{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.hardware.asus-zenbook-a16.edpRetrain;
  inherit (lib) mkOption types;
in
{
  options.hardware.asus-zenbook-a16.edpRetrain = {
    enable = mkOption {
      type = types.bool;
      default = true;
      description = ''
        At boot, check the eDP link msm trained and, if it fell back below
        expectedLanes, retrain it with an fbdev blank/unblank.
      '';
    };
    attempts = mkOption {
      type = types.ints.positive;
      default = 5;
      description = "Retrains to try before giving up.";
    };
    expectedLanes = mkOption {
      type = types.ints.positive;
      default = 4;
      description = "Lane count a good link has (the panel needs x4 for 2880x1800@120).";
    };
    timeout = mkOption {
      type = types.ints.positive;
      default = 90;
      description = "Seconds to wait for msm to bring up eDP-1 and fb0 before giving up.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.services.lab-edp-retrain = {
      description = "Retrain the eDP link if msm fell back to fewer lanes";
      wantedBy = [ "multi-user.target" ];
      after = [ "sys-kernel-debug.mount" ];
      wants = [ "sys-kernel-debug.mount" ];
      restartIfChanged = false;
      path = [
        pkgs.coreutils
        pkgs.gawk
        pkgs.gnugrep
        pkgs.util-linux
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      environment = {
        ATTEMPTS = toString cfg.attempts;
        EXPECTED = toString cfg.expectedLanes;
        TIMEOUT = toString cfg.timeout;
      };
      script = ''
        # Kernel log at err: the console loglevel is 4, so lower levels don't
        # reach serial/netconsole.
        log() { echo "$*"; echo "<3>lab-edp-retrain: $*" > /dev/kmsg || true; }
        fb=/sys/class/graphics/fb0/blank
        # Failed attempts (warnings) and give-ups (errors) for eDP-1; see
        # the contract at the top of modules/zenbook/edp-retrain.nix.
        fails() { dmesg | grep -c 'eDP-1: link training attempt [0-9]* failed' || true; }
        gaveup() { dmesg | grep -cE 'eDP-1: link training( before stream on)? failed after' || true; }

        conn= dbg= up=
        for _ in $(seq "$TIMEOUT"); do
          conn=$(ls -d /sys/class/drm/card*-eDP-1 2>/dev/null | head -n1 || true)
          if [ -n "$conn" ] && [ -e "$fb" ] && [ "$(cat "$conn/dpms" 2>/dev/null)" = On ]; then
            up=1
            break
          fi
          sleep 1
        done
        if [ -z "$up" ]; then
          log "eDP-1/fb0 not up after $TIMEOUT s; nothing done"
          exit 0
        fi
        for _ in $(seq 10); do
          dbg=$(ls /sys/kernel/debug/dri/*/eDP-1/dp_debug 2>/dev/null | head -n1 || true)
          [ -n "$dbg" ] && break
          sleep 1
        done
        [ -n "$dbg" ] || log "no dp_debug in debugfs; judging by kernel log errors only"
        sleep 2  # let the first modeset's training finish

        lanes() {
          local l=
          [ -n "$dbg" ] && l=$(awk '/dp_link:/ { f = 1 } f && /num_lanes/ { print $3; exit }' "$dbg")
          case "$l" in "" | *[!0-9]*) echo "?" ;; *) echo "$l" ;; esac
        }
        # Good: no give-up since the check whose give-up count is $1, and
        # the lanes, if known, are enough.
        good() {
          local l
          l=$(lanes)
          [ "$(gaveup)" -le "$1" ] || return 1
          [ "$l" = "?" ] || [ "$l" -ge "$EXPECTED" ]
        }

        # Nothing retrains eDP between boot and here (no compositor yet), so
        # any give-up so far means the panel is dark now.
        before=$(lanes)
        if good 0; then
          log "eDP link ok: lanes=$before (expected $EXPECTED), failed attempts so far $(fails); nothing to do"
          exit 0
        fi

        log "eDP link degraded: lanes=$before (expected $EXPECTED), failed attempts $(fails), give-ups $(gaveup); retraining via fb0 blank/unblank"
        for i in $(seq "$ATTEMPTS"); do
          base=$(gaveup)
          if ! echo 1 > "$fb"; then
            log "attempt $i/$ATTEMPTS: blank failed (DRM master present?)"
          fi
          sleep 0.5
          if ! echo 0 > "$fb"; then
            log "attempt $i/$ATTEMPTS: unblank failed"
          fi
          sleep 1.5
          if good "$base"; then
            log "attempt $i/$ATTEMPTS: lanes=$(lanes), link retrained ok"
            exit 0
          fi
          log "attempt $i/$ATTEMPTS: lanes=$(lanes), failed attempts $(fails), give-ups $(gaveup); retrying"
          sleep 1
        done
        log "giving up after $ATTEMPTS attempts: lanes=$(lanes)"
        exit 1
      '';
    };

    # The fbdev blank/unblank only retrains while no compositor is DRM
    # master, so a greeter that runs one (SDDM's kwin_wayland or weston,
    # GDM's gnome-shell) waits for it: ~14 s on a normal boot, at most
    # `timeout` if eDP never comes up. Text greeters (greetd with tuigreet,
    # getty) run on fbcon and need no ordering; a session started from them
    # within those seconds can still preempt the retrain.
    systemd.services.display-manager =
      lib.mkIf (config.services.displayManager.sddm.enable || config.services.displayManager.gdm.enable)
        {
          after = [ "lab-edp-retrain.service" ];
          wants = [ "lab-edp-retrain.service" ];
        };
  };
}
