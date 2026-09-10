#!/bin/sh
set -u

set_gnome_value() {
  schema="$1"
  key="$2"
  value="$3"
  if command -v gsettings >/dev/null 2>&1 \
    && gsettings list-schemas 2>/dev/null | grep -Fxq "$schema" \
    && gsettings list-keys "$schema" 2>/dev/null | grep -Fxq "$key"; then
    gsettings set "$schema" "$key" "$value" >/dev/null 2>&1 || true
  fi
}

# Disable GNOME's idle lock and automatic suspend for the dedicated VNC
# operations desktop. VNC transport authentication remains separate.
set_gnome_value org.gnome.desktop.session idle-delay 'uint32 0'
set_gnome_value org.gnome.desktop.screensaver lock-enabled false
set_gnome_value org.gnome.desktop.screensaver idle-activation-enabled false
set_gnome_value org.gnome.desktop.screensaver lock-delay 'uint32 0'
set_gnome_value org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type "'nothing'"
set_gnome_value org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type "'nothing'"
set_gnome_value org.gnome.settings-daemon.plugins.power idle-dim false

if command -v xset >/dev/null 2>&1; then
  xset s off >/dev/null 2>&1 || true
  xset s noblank >/dev/null 2>&1 || true
  xset -dpms >/dev/null 2>&1 || true
fi
