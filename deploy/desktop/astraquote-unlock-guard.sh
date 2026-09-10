#!/bin/sh
set -u

TARGET_UID=$(id -u)
TARGET_BUS="unix:path=/run/user/$TARGET_UID/bus"

unlock_vnc_sessions() {
  loginctl list-sessions --no-legend 2>/dev/null \
    | while read -r session_id session_uid _rest; do
        [ "$session_uid" = "$TARGET_UID" ] || continue
        [ "$(loginctl show-session "$session_id" -p Service --value 2>/dev/null)" = "tigervnc" ] \
          || continue
        [ "$(loginctl show-session "$session_id" -p LockedHint --value 2>/dev/null)" = "yes" ] \
          || continue
        loginctl unlock-session "$session_id" >/dev/null 2>&1 || true
      done
}

# GNOME can still receive an explicit lock request even when idle locking is
# disabled. This desktop is reachable only through the authenticated VNC-over-
# SSH channel, so immediately clear any lock state created inside that session.
while :; do
  unlock_vnc_sessions
  if [ -S "/run/user/$TARGET_UID/bus" ]; then
    DBUS_SESSION_BUS_ADDRESS="$TARGET_BUS" \
      gdbus call --session \
        --dest org.gnome.ScreenSaver \
        --object-path /org/gnome/ScreenSaver \
        --method org.gnome.ScreenSaver.SetActive false \
        >/dev/null 2>&1 || true
  fi
  sleep 2
done
