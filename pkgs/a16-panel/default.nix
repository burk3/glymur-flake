{
  writeShellApplication,
  procps,
  systemd,
  util-linux,
}:

# `a16-panel off|on` (root): DPMS the panel through the compositor of the
# active graphical session, for lab tests that don't need the screen (the
# OLED otherwise sits lit). The compositors turn it back on by themselves on
# the next key or touchpad input. niri and Hyprland are driven with their own
# binary (the running compositor's, so the IPC matches its version); KWin
# with kscreen-doctor from the system's PATH. Nothing is installed for any
# of them.
writeShellApplication {
  name = "a16-panel";
  runtimeInputs = [
    procps
    systemd
    util-linux
  ];
  text = ''
    case "''${1:-}" in
      off | on) state=$1 ;;
      *)
        echo "usage: a16-panel off|on" >&2
        exit 2
        ;;
    esac

    uid=""
    for s in $(loginctl list-sessions --no-legend | awk '{print $1}'); do
      if [ "$(loginctl show-session "$s" -p Type --value)" = wayland ] &&
        [ "$(loginctl show-session "$s" -p Active --value)" = yes ]; then
        uid=$(loginctl show-session "$s" -p User --value)
      fi
    done
    if [ -z "$uid" ]; then
      echo "a16-panel: no active Wayland session" >&2
      exit 1
    fi
    user=$(id -nu "$uid")
    rt=/run/user/$uid
    as_user() { runuser -u "$user" -- env XDG_RUNTIME_DIR="$rt" "$@"; }

    # niri: its socket is $XDG_RUNTIME_DIR/niri.<wayland display>.<pid>.sock.
    for pid in $(pgrep -u "$uid" -x niri || true); do
      for sock in "$rt"/niri.*."$pid".sock; do
        [ -S "$sock" ] || continue
        exec runuser -u "$user" -- env NIRI_SOCKET="$sock" \
          "$(readlink "/proc/$pid/exe")" msg action "power-$state-monitors"
      done
    done

    # Hyprland: $XDG_RUNTIME_DIR/hypr/<instance>/hyprland.lock starts with its
    # pid; hyprctl sits next to the Hyprland binary.
    for pid in $(pgrep -u "$uid" -x Hyprland || true); do
      for lock in "$rt"/hypr/*/hyprland.lock; do
        [ -f "$lock" ] && [ "$(head -n1 "$lock")" = "$pid" ] || continue
        sig=$(basename "$(dirname "$lock")")
        exec runuser -u "$user" -- env XDG_RUNTIME_DIR="$rt" HYPRLAND_INSTANCE_SIGNATURE="$sig" \
          "$(dirname "$(readlink "/proc/$pid/exe")")/hyprctl" dispatch dpms "$state"
      done
    done

    # KWin (the Plasma session or SDDM's greeter).
    if pgrep -u "$uid" -x kwin_wayland >/dev/null && command -v kscreen-doctor >/dev/null; then
      display=wayland-0
      for sock in "$rt"/wayland-[0-9]*; do
        [ -S "$sock" ] && display=$(basename "$sock") && break
      done
      as_user WAYLAND_DISPLAY="$display" QT_QPA_PLATFORM=wayland kscreen-doctor --dpms "$state"
      exit
    fi

    echo "a16-panel: no niri, Hyprland or KWin (with kscreen-doctor) in $user's session" >&2
    exit 1
  '';
}
