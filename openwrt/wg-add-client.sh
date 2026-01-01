#!/bin/sh
# wg-add-client.sh - interactive WireGuard client manager (OpenWrt BusyBox ash)

set -eu

VPN_IF="wg0"
WG_DIR="/etc/wireguard"
CLIENT_PREFIX="wgclient"

NET_V4="10.7.0"
SERVER_HOST_V4=1
NET_V6_PREFIX="fd00:7::"

LISTEN_PORT_DEFAULT="51820"
DNS_V4="192.168.1.1"

say() { printf '%s\n' "$*"; }
err() { printf 'ERROR: %s\n' "$*" >&2; }
die() { err "$*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

need uci
need wg
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
need ifup
need ifdown
need sed
need ubus

umask 077
mkdir -p "$WG_DIR"
chmod 700 "$WG_DIR"

digits_only() { printf '%s' "$1" | tr -cd '0-9'; }

pause_any_key() {
  say ""
  printf '%s' "Press any key to continue..."
  if read -r -n 1 _K 2>/dev/null; then :; else read -r _K || true; fi
  say ""
}

clear_screen() {
  if command -v clear >/dev/null 2>&1; then
    clear
  else
    printf '\033[2J\033[H' 2>/dev/null || true
    i=0; while [ "$i" -lt 40 ]; do say ""; i=$((i+1)); done
  fi
}

clean_section() { printf '%s' "$1" | tr -d '\r' | tr -cd 'A-Za-z0-9_'; }

client_sections_nl() {
  uci show network 2>/dev/null \
    | tr -d '\r' \
    | grep -E "^network\\.${CLIENT_PREFIX}[0-9]+=wireguard_${VPN_IF}$" \
    | cut -d. -f2 \
    | cut -d= -f1 \
    | sort -V
}

configured_peer_count() {
  c="$(client_sections_nl | grep -c . | tr -cd '0-9')"
  [ -n "$c" ] || c=0
  echo "$c"
}

runtime_peer_count() {
  c="$(wg show "$VPN_IF" 2>/dev/null | grep -c '^peer:' | tr -cd '0-9')"
  [ -n "$c" ] || c=0
  echo "$c"
}

ifstatus_up() {
  # best indicator: ifstatus wg0 -> "up": true/false
  up="$(ifstatus "$VPN_IF" 2>/dev/null | tr -d '\r' | grep -m1 '"up"' | grep -oE 'true|false' || true)"
  [ -n "$up" ] && echo "$up" || echo "n/a"
}

wg_addr_v4() {
  ip address show dev "$VPN_IF" 2>/dev/null | grep -m1 ' inet ' | tr -s ' ' | cut -d' ' -f3 || true
}

wg_addr_v6() {
  ip address show dev "$VPN_IF" 2>/dev/null | grep -m1 ' inet6 ' | grep -v ' fe80:' | tr -s ' ' | cut -d' ' -f3 || true
}

server_pubkey() {
  if [ -s "$WG_DIR/server.pub" ]; then
    cat "$WG_DIR/server.pub"
    return 0
  fi
  wg show "$VPN_IF" 2>/dev/null | grep -m1 'public key:' | tr -s ' ' | cut -d' ' -f3- || true
}

listen_port_runtime() {
  p="$(wg show "$VPN_IF" 2>/dev/null | grep -m1 'listening port:' | tr -s ' ' | cut -d' ' -f3)"
  [ -n "$p" ] && echo "$p" || echo ""
}

listen_port_config() {
  p="$(uci -q get network.$VPN_IF.listen_port 2>/dev/null || true)"
  p="$(printf '%s' "$p" | tr -cd '0-9')"
  [ -n "$p" ] && echo "$p" || echo "$LISTEN_PORT_DEFAULT"
}

endpoint_guess() {
  EP="$(ip -4 -o addr show dev pppoe-wan 2>/dev/null | head -n1 | tr -s ' ' | cut -d' ' -f4 | cut -d/ -f1)"
  [ -n "$EP" ] && echo "$EP" || echo "<router-public-ip-or-ddns>"
}

wg_reload() {
  # Prefer a soft reload, but fall back to ifdown/ifup
  ubus call network.interface."$VPN_IF" reload >/dev/null 2>&1 && { sleep 1; return 0; }
  service network reload >/dev/null 2>&1 && { sleep 1; return 0; }
  ifdown "$VPN_IF" >/dev/null 2>&1 || true
  ifup "$VPN_IF" >/dev/null 2>&1 || true
  sleep 2
}

wg_reload_ensure_peers() {
  # After changes, ensure runtime peers match configured peers
  wg_reload
  conf="$(configured_peer_count)"
  run="$(runtime_peer_count)"
  if [ "$conf" != "$run" ]; then
    ifdown "$VPN_IF" >/dev/null 2>&1 || true
    sleep 2
    ifup "$VPN_IF" >/dev/null 2>&1 || true
    sleep 2
  fi
}

show_status() {
  say ""
  say "=== WireGuard status (${VPN_IF}) ==="

  if ! ip link show "$VPN_IF" >/dev/null 2>&1; then
    say "Interface: missing"
    say "Up:        false"
    say "Configured peers: $(configured_peer_count)"
    say "Runtime peers:    0"
    say "==============================="
    say ""
    return 0
  fi

  A4="$(wg_addr_v4)"; [ -n "$A4" ] || A4="(none)"
  A6="$(wg_addr_v6)"; [ -n "$A6" ] || A6="(none)"

  rp="$(listen_port_runtime)"
  cp="$(listen_port_config)"

  say "Interface: present"
  say "Up:        $(ifstatus_up)"
  say "IPv4:      $A4"
  say "IPv6:      $A6"
  say "Listen:    runtime: ${rp:-"(none)"} | config: $cp"
  say "ServerKey: $(server_pubkey)"
  say "Configured peers: $(configured_peer_count)"
  say "Runtime peers:    $(runtime_peer_count)"
  say "==============================="
  say ""
}

list_clients() {
  say ""
  say "=== Clients ==="
  SECS="$(client_sections_nl || true)"
  if [ -z "$SECS" ]; then
    say "No clients found."
    return 0
  fi

  printf '%s\n' "$SECS" | while IFS= read -r S; do
    S="$(clean_section "$S")"
    [ -n "$S" ] || continue
    ALW="$(uci -q get network.${S}.allowed_ips 2>/dev/null || true)"
    say "- ${S}: ${ALW}"
    CONF="$WG_DIR/${S}.conf"
    [ -f "$CONF" ] && say "  conf: $CONF"
  done
}

client_host_v4() {
  SEC="$1"
  ALW="$(uci -q get network.${SEC}.allowed_ips 2>/dev/null || true)"
  [ -n "$ALW" ] || return 0

  for tok in $ALW; do
    case "$tok" in
      ${NET_V4}.*\/32)
        iponly="${tok%/32}"
        host="${iponly##*.}"
        case "$host" in
          ''|*[!0-9]*) ;;
          *) echo "$host"; return 0 ;;
        esac
        ;;
    esac
  done
  return 0
}

pick_next_ipv4_host() {
  USED="$(
    for S in $(client_sections_nl); do
      S="$(clean_section "$S")"
      [ -n "$S" ] || continue
      H="$(client_host_v4 "$S" || true)"
      [ -n "$H" ] && echo "$H"
    done | sort -n | uniq
  )"

  for H in $(seq 2 254); do
    [ "$H" -eq "$SERVER_HOST_V4" ] && continue
    echo "$USED" | grep -qx "$H" && continue
    echo "$H"
    return 0
  done
  return 1
}

next_client_index() {
  # IMPORTANT: no pipelines here (avoid subshell variable loss)
  max=0
  for S in $(client_sections_nl); do
    S="$(clean_section "$S")"
    n="${S#${CLIENT_PREFIX}}"
    case "$n" in
      ''|*[!0-9]*) continue ;;
    esac
    [ "$n" -gt "$max" ] && max="$n"
  done
  echo $((max + 1))
}

add_new_client() {
  [ -s "$WG_DIR/server.pub" ] || { err "Missing $WG_DIR/server.pub - generate server keys first."; return 1; }

  # Prompt for peer name/description
  say ""
  say "Enter peer name (letters, numbers, dash, underscore - e.g., John-Phone, Office_Laptop):"
  printf '%s' "Peer name: "
  read -r PEER_NAME || return 1
  
  # Validate: must contain only alphanumeric characters, dash, and underscore
  CLEANED="$(printf '%s' "$PEER_NAME" | tr -cd 'A-Za-z0-9_-')"
  if [ "$PEER_NAME" != "$CLEANED" ]; then
    err "Invalid peer name - only letters, numbers, dash (-), and underscore (_) allowed."
    return 1
  fi
  
  if [ -z "$PEER_NAME" ]; then
    err "Invalid peer name - must contain at least one character."
    return 1
  fi
  
  if [ "${#PEER_NAME}" -gt 64 ]; then
    err "Peer name too long (max 64 characters)."
    return 1
  fi

  # Check if description already exists
  for S in $(client_sections_nl); do
    S="$(clean_section "$S")"
    [ -n "$S" ] || continue
    EXISTING_DESC="$(uci -q get network.${S}.description 2>/dev/null || true)"
    if [ -n "$EXISTING_DESC" ]; then
      # Case-insensitive comparison
      EXISTING_LOWER="$(printf '%s' "$EXISTING_DESC" | tr 'A-Z' 'a-z')"
      NEW_LOWER="$(printf '%s' "$PEER_NAME" | tr 'A-Z' 'a-z')"
      if [ "$EXISTING_LOWER" = "$NEW_LOWER" ]; then
        err "Peer name '${PEER_NAME}' already exists - choose a different name."
        return 1
      fi
    fi
  done

  HOST_V4="$(pick_next_ipv4_host)" || { err "No free IPv4 host in ${NET_V4}.0/24"; return 1; }
  CLIENT_V4="${NET_V4}.${HOST_V4}"
  CLIENT_V6="${NET_V6_PREFIX}${HOST_V4}"

  N="$(next_client_index)"
  SECTION="${CLIENT_PREFIX}${N}"

  C_KEY="$WG_DIR/${PEER_NAME}.key"
  C_PUB="$WG_DIR/${PEER_NAME}.pub"
  C_PSK="$WG_DIR/${PEER_NAME}.psk"
  C_CONF="$WG_DIR/${PEER_NAME}.conf"

  say ""
  say "Adding peer '${PEER_NAME}' (${SECTION}) with ${CLIENT_V4}/32 and ${CLIENT_V6}/128"

  PRIV="$(wg genkey)" || { err "Key generation failed"; return 1; }
  printf '%s\n' "$PRIV" > "$C_KEY"
  printf '%s\n' "$PRIV" | wg pubkey > "$C_PUB"
  wg genpsk > "$C_PSK"
  chmod 600 "$C_KEY" "$C_PSK"

  # refuse to overwrite an existing section (safety)
  if uci -q get network."$SECTION" >/dev/null 2>&1; then
    err "Section $SECTION already exists - refusing to overwrite."
    return 1
  fi

  uci set network."$SECTION"="wireguard_${VPN_IF}"
  uci set network."$SECTION".description="${PEER_NAME}"
  uci set network."$SECTION".public_key="$(cat "$C_PUB")"
  uci set network."$SECTION".preshared_key="$(cat "$C_PSK")"
  uci -q delete network."$SECTION".allowed_ips || true
  uci add_list "network.$SECTION.allowed_ips=${CLIENT_V4}/32"
  uci add_list "network.$SECTION.allowed_ips=${CLIENT_V6}/128"
  uci commit network

  wg_reload_ensure_peers

  EP="$(endpoint_guess)"
  PORT="$(listen_port_config)"

  cat > "$C_CONF" <<EOC
[Interface]
PrivateKey = $(cat "$C_KEY")
Address = ${CLIENT_V4}/32, ${CLIENT_V6}/128
DNS = ${DNS_V4}

[Peer]
PublicKey = $(cat "$WG_DIR/server.pub")
PresharedKey = $(cat "$C_PSK")
Endpoint = ${EP}:${PORT}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
EOC
  chmod 600 "$C_CONF"

  say "Done."
  say "Peer name: ${PEER_NAME}"
  say "Client config: $C_CONF"
}

remove_client() {
  SECS="$(client_sections_nl || true)"
  [ -n "$SECS" ] || { err "No clients to remove."; return 1; }

  say ""
  say "Select client number to remove (0 = cancel):"
  # no pipeline here to avoid subshell variable loss
  i=1
  for S in $(client_sections_nl); do
    S="$(clean_section "$S")"
    [ -n "$S" ] || continue
    ALW="$(uci -q get network.${S}.allowed_ips 2>/dev/null || true)"
    say "${i}) ${S}  ${ALW}"
    i=$((i+1))
  done

  printf '%s' "Enter selection number: "
  read -r RAW || return 1
  CHOICE="$(digits_only "$RAW")"
  [ -n "$CHOICE" ] || { err "Numbers only."; return 1; }

  if [ "$CHOICE" -eq 0 ]; then
    say "Canceled."
    return 0
  fi

  COUNT="$(printf '%s\n' "$SECS" | grep -c . | tr -cd '0-9')"
  if [ "$CHOICE" -lt 1 ] || [ "$CHOICE" -gt "$COUNT" ]; then
    err "Selection out of range."
    return 1
  fi

  TARGET="$(printf '%s\n' "$SECS" | sed -n "${CHOICE}p")"
  TARGET="$(clean_section "$TARGET")"
  [ -n "$TARGET" ] || { err "Cannot resolve selection."; return 1; }

  say ""
  say "Removing ${TARGET} ..."

  uci -q delete network."$TARGET" || true
  uci commit network

  wg_reload_ensure_peers

  rm -f "$WG_DIR/${TARGET}.key" "$WG_DIR/${TARGET}.pub" "$WG_DIR/${TARGET}.psk" "$WG_DIR/${TARGET}.conf" 2>/dev/null || true
  say "Removed ${TARGET}."
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

