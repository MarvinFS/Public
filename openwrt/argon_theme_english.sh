#!/bin/sh

# ====================================================================
# Argon Theme Installer for OpenWrt
# ====================================================================
# IMPORTANT: This script uses pre-built packages for OpenWrt 24.10.5
# For newer OpenWrt versions, use packages from the original repository:
# https://github.com/jerrykuku/luci-theme-argon
# https://github.com/jerrykuku/luci-app-argon-config
# ====================================================================

# ==== HELPERS ======================================================
pause_key() {
  echo ""; while read -r -t 0.01 _ 2>/dev/null; do :; done
  read -n1 -s -r -p "Press any key to return to menu..."; echo ""
}
ask_back_menu() {
  echo ""; read -r -p "Return to menu? [y/N]: " back; [ "$back" = "y" ];
}
color() {
  case "$1" in
    green) printf "\033[32m%s\033[0m" "$2";;
    red)   printf "\033[31m%s\033[0m" "$2";;
    reset) printf "\033[0m%s" "$2";;
  esac
}

# ==== EXIT FUNCTION ==============================================
bye_msg() {
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Flint 2 Community (ONLY IN RUSSIAN!!!): configuration help, support, ready-made scripts and secret features."
  echo "👉 https://t.me/flint_2"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# display bye_msg on any exit (EXIT or Ctrl+C)
trap bye_msg EXIT

# ==== INFO ========================================================
LAN_IP="$( (uci get network.lan.ipaddr 2>/dev/null) || hostname -I | awk '{print $1}' || echo 127.0.0.1)"
PORT=80
CURRENT_THEME="bootstrap"

if opkg list-installed | grep -q luci-theme-argon; then
  CURRENT_THEME="argon"
fi

# ==== FUNCTIONS =====================================================
show_menu() {
  clear 2>/dev/null || printf '\033c'
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo " Argon Theme Installer"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  printf " Current theme: %s\n" "$( [ "$CURRENT_THEME" = "argon" ] && color green 'argon' || color red 'bootstrap' )"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  printf " %-2s) %s\n" "1" "Install Argon theme"
  printf " %-2s) %s\n" "2" "Restore Bootstrap theme and remove Argon"
  printf " %-2s) %s\n" "0" "Exit"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  printf "Select option: "
}

install_argon() {
  if [ "$CURRENT_THEME" = "argon" ]; then
    echo "$(color green 'Argon theme is already installed. Skipping.')"
    return
  fi

  # Check if wget is available
  if ! command -v wget >/dev/null 2>&1; then
    echo "$(color red 'Error: wget is not installed.')"
    echo "Install wget first: opkg update && opkg install wget"
    return
  fi

  echo "Updating package lists..."
  opkg update

  echo "Installing dependencies..."
  opkg install luci-compat luci-lib-ipkg || {
    echo "$(color red 'Error: dependencies not installed.')"
    return
  }

  echo "Downloading Argon theme..."
  wget -O /tmp/luci-theme-argon.ipk https://github.com/jerrykuku/luci-theme-argon/releases/download/v2.3.2/luci-theme-argon_2.3.2-r20250207_all.ipk || {
    echo "$(color red 'Error: failed to download theme package.')"
    return
  }
  opkg install /tmp/luci-theme-argon.ipk || {
    echo "$(color red 'Error: theme not installed.')"
    rm -f /tmp/luci-theme-argon*.ipk
    return
  }

  echo "Installing configurator..."
  if wget -O /tmp/luci-app-argon-config.ipk https://github.com/jerrykuku/luci-app-argon-config/releases/download/v0.9/luci-app-argon-config_0.9_all.ipk; then
    opkg install /tmp/luci-app-argon-config.ipk || {
      echo "Configurator may work with limitations."
    }
  else
    echo "$(color red 'Warning: failed to download configurator. Theme will work without it.')"
  fi

  rm -f /tmp/luci-theme-argon*.ipk
  echo "$(color green 'Installation completed.')"
  echo "Select theme in LuCI: System → System → Design"

  echo ""
  echo "Install Russian language for LuCI? [y/N]"
  read -r lang
  if [ "$lang" = "y" ] || [ "$lang" = "Y" ]; then
    opkg install luci-i18n-base-ru && echo "$(color green 'Russian language installed.')"
  else
    echo "Skipping language installation."
  fi
}

revert_to_default() {
  echo "Reverting to Bootstrap theme..."
  uci set luci.main.mediaurlbase='/luci-static/bootstrap'
  uci commit luci
  /etc/init.d/uhttpd restart
  echo "$(color green 'Bootstrap activated.')"

  echo "Removing Argon theme..."
  opkg remove luci-theme-argon luci-app-argon-config 2>/dev/null
  rm -f /tmp/luci-theme-argon*.ipk
  echo "$(color green 'Cleanup completed.')"
}

# ==== MAIN LOOP ==============================================
trap 'echo; echo "Press 0 to exit."' INT

while true; do
  show_menu
  read -r CHOICE
  case "$CHOICE" in
    1) install_argon; ask_back_menu || break;;
    2) revert_to_default; ask_back_menu || break;;
    0) echo "Exiting."; break;;
    *) echo "$(color red 'Invalid choice. Please try again.')"; pause_key;;
  esac
  echo ""
done
