#!/bin/sh
# wg-rebuild-server-and-all-clients.sh - interactive WG client manager (OpenWrt BusyBox ash)
# Menu:
# 1) list clients
# 2) add new (next free IPv4 host in 10.7.0.0/24)
# 3) remove (by client number only)
# 4) exit
#
# UX:
# - After each action: "Press any key to continue..."
# - Clear screen when returning to main menu

set -eu

VPN_IF="wg0"
WG_DIR="/etc/wireguard"
CLIENT_PREFIX="wgclient"

NET_V4="10.7.0"           # 10.7.0.0/24
SERVER_HOST_V4=1          # 10.7.0.1
NET_V6_PREFIX="fd00:7::"  # fd00:7::/64

LISTEN_PORT="51820"
DNS_V4="192.168.1.1"

say() { printf '%s\n' "$*"; }
err() { printf 'ERROR: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

need uci
need wg
need awk
need ip
need tr
need cut
need sort
need uniq
need grep
need wc
need head
need seq
need tee
need sleep
need sed

umask 077
mkdir -p "$WG_DIR"
chmod 700 "$WG_DIR"

digits_only() { printf '%s' "$1" | tr -cd '0-9'; }

pause_any_key() {
  say ""
  printf '%s' "Press any key to continue..."
  # Read a single character (BusyBox ash supports -n)
  # If -n is not supported for some reason, fallback to Enter.
  if read -r -n 1 _K 2>/dev/null; then
    :
  else
    read -r _K || true
  fi
  say ""
}

clear_screen() {
  # Prefer ANSI clear, fallback to printing newlines
  if command -v clear >/dev/null 2>&1; then
    clear
  else
    printf '\033[2J\033[H' 2>/dev/null || true
    # If ANSI not supported, at least push old content away
    i=0; while [ "$i" -lt 40 ]; do say ""; i=$((i+1)); done
  fi
}

# Return wgclientN section names for this WG interface
client_sections() {
  uci show network 2>/dev/null | awk -v p="$CLIENT_PREFIX" -v ifn="$VPN_IF" -F'=' '
    $2=="wireguard_"ifn {
      split($1,a,"."); s=a[2];
      if (s ~ "^"p"[0-9]+$") print s;
    }' | sort -V
}

clean_section() { printf '%s' "$1" | tr -cd 'A-Za-z0-9_'; }

endpoint_guess() {
  EP="$(ip -4 addr show dev pppoe-wan 2>/dev/null | awk '/inet /{print $2}' | head -n1 | cut -d/ -f1)"
  [ -n "$EP" ] && echo "$EP" || echo "<router-public-ip-or-ddns>"
}

show_status() {
  say ""
  say "=== Service status ==="
  if ip link show "$VPN_IF" >/dev/null 2>&1; then
    ip -br link show "$VPN_IF" || true
    ip -br addr show "$VPN_IF" || true
  else
    say "Interface $VPN_IF not present."
  fi
  say ""
  wg show || true
  say "======================"
  say ""
}

list_clients() {
  say ""
  say "=== Clients ==="
  SECS="$(client_sections || true)"
  if [ -z "$SECS" ]; then
    say "No clients found."
    return 0
  fi

  for S in $SECS; do
    S="$(clean_section "$S")"
    ALW="$(uci -q get network.${S}.allowed_ips 2>/dev/null || true)"
    say "- ${S}: ${ALW}"
    CONF="$WG_DIR/${S}.conf"
    [ -f "$CONF" ] && say "  conf: $CONF"
  done
  return 0
}

pick_next_ipv4_host() {
  USED_V4="$(
    uci show network 2>/dev/null \
      | grep -E "^network\.${CLIENT_PREFIX}[0-9]+\.allowed_ips=" \
      | tr -d "'" \
      | sed 's/.*allowed_ips=//' \
      | tr ' ' '\n' \
      | awk -v n="$NET_V4" '
          $0 ~ "^"n"\\.[0-9]+/32$" {
            sub(/.*\\./,"",$0);
            sub(/\\/32$/,"",$0);
            print $0
          }' \
      | sort -n | uniq
  )"

  for H in $(seq 2 254); do
    [ "$H" -eq "$SERVER_HOST_V4" ] && continue
    echo "$USED_V4" | grep -qx "$H" && continue
    echo "$H"
    return 0
  done
  return 1
}

next_client_index() {
  MAXN=0
  for S in $(client_sections || true); do
    S="$(clean_section "$S")"
    N="$(echo "$S" | sed "s/^${CLIENT_PREFIX}//")"
    echo "$N" | grep -Eq '^[0-9]+$' || continue
    [ "$N" -gt "$MAXN" ] && MAXN="$N"
  done
  echo $((MAXN + 1))
}

add_new_client() {
  [ -s "$WG_DIR/server.pub" ] || { err "Missing $WG_DIR/server.pub - generate server keys first."; return 1; }

  HOST_V4="$(pick_next_ipv4_host)" || { err "No free IPv4 host in ${NET_V4}.0/24"; return 1; }
  CLIENT_V4="${NET_V4}.${HOST_V4}"
  CLIENT_V6="${NET_V6_PREFIX}${HOST_V4}"

  N="$(next_client_index)"
  SECTION="${CLIENT_PREFIX}${N}"

  C_KEY="$WG_DIR/${SECTION}.key"
  C_PUB="$WG_DIR/${SECTION}.pub"
  C_PSK="$WG_DIR/${SECTION}.psk"
  C_CONF="$WG_DIR/${SECTION}.conf"

  say ""
  say "Adding ${SECTION} with ${CLIENT_V4}/32 and ${CLIENT_V6}/128"

  PRIV="$(wg genkey)" || { err "Key generation failed"; return 1; }
  printf '%s\n' "$PRIV" > "$C_KEY"
  printf '%s\n' "$PRIV" | wg pubkey > "$C_PUB"
  wg genpsk > "$C_PSK"
  chmod 600 "$C_KEY" "$C_PSK"

  uci -q delete network."$SECTION" || true
  uci set network."$SECTION"="wireguard_${VPN_IF}"
  uci set network."$SECTION".public_key="$(cat "$C_PUB")"
  uci set network."$SECTION".preshared_key="$(cat "$C_PSK")"
  uci -q delete network."$SECTION".allowed_ips || true
  uci add_list "network.$SECTION.allowed_ips=${CLIENT_V4}/32"
  uci add_list "network.$SECTION.allowed_ips=${CLIENT_V6}/128"

  uci commit network
  service network restart >/dev/null 2>&1 || true
  sleep 1

  EP="$(endpoint_guess)"

  cat > "$C_CONF" <<EOC
[Interface]
PrivateKey = $(cat "$C_KEY")
Address = ${CLIENT_V4}/32, ${CLIENT_V6}/128
DNS = ${DNS_V4}

[Peer]
PublicKey = $(cat "$WG_DIR/server.pub")
PresharedKey = $(cat "$C_PSK")
Endpoint = ${EP}:${LISTEN_PORT}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
EOC
  chmod 600 "$C_CONF"

  say "Done. Client config: $C_CONF"
  return 0
}

remove_client() {
  SECS="$(client_sections || true)"
  if [ -z "$SECS" ]; then
    err "No clients to remove."
    return 1
  fi

  say ""
  say "Select client number to remove:"

  I=1
  for S in $SECS; do
    S="$(clean_section "$S")"
    ALW="$(uci -q get network.${S}.allowed_ips 2>/dev/null || true)"
    say "${I}) ${S}  ${ALW}"
    I=$((I + 1))
  done

  printf '%s' "Enter selection number: "
  read -r RAW || return 1
  CHOICE="$(digits_only "$RAW")"
  CHOICE="$(printf '%s' "$CHOICE" | cut -c1-6)" # avoid absurdly long numbers
  [ -n "$CHOICE" ] || { err "Numbers only."; return 1; }

  COUNT="$(echo "$SECS" | wc -w | tr -cd '0-9')"
  [ -n "$COUNT" ] || COUNT=0

  if [ "$CHOICE" -lt 1 ] || [ "$CHOICE" -gt "$COUNT" ]; then
    err "Selection out of range."
    return 1
  fi

  TARGET="$(echo "$SECS" | awk -v n="$CHOICE" 'NR==n{print;exit}')"
  TARGET="$(clean_section "$TARGET")"
  [ -n "$TARGET" ] || { err "Cannot resolve selection."; return 1; }

  say ""
  say "Removing ${TARGET} ..."

  uci -q delete network."$TARGET" || true
  uci commit network
  service network restart >/dev/null 2>&1 || true
  sleep 1

  rm -f "$WG_DIR/${TARGET}.key" "$WG_DIR/${TARGET}.pub" "$WG_DIR/${TARGET}.psk" "$WG_DIR/${TARGET}.conf" 2>/dev/null || true

  say "Removed ${TARGET}."
  return 0
}

main_menu() {
  while :; do
    clear_screen
    show_status
    say "1. list clients"
    say "2. add new"
    say "3. remove"
    say "4. exit"
    printf '%s' "Select: "
    read -r RAW || exit 0
    CH="$(digits_only "$RAW" | cut -c1)"

    case "$CH" in
      1) list_clients; pause_any_key ;;
      2) add_new_client || true; pause_any_key ;;
      3) remove_client || true; pause_any_key ;;
      4) exit 0 ;;
      *) err "Invalid choice."; pause_any_key ;;
    esac
  done
}

main_menu
