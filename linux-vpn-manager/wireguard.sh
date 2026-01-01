#!/bin/bash
#
# WireGuard VPN - Install & Manage (Combined)
#
# Based on wireguard-install by angristan
# https://github.com/angristan/wireguard-install
# Modified and integrated into Linux VPN Manager
#

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" || { echo "ERROR: common.sh not found"; exit 1; }

WG_DIR="/etc/wireguard"
WG_PARAMS="${WG_DIR}/params"
CLIENT_DIR="/etc/vpn/wireguard/clients"

# ============================================================================
# INSTALLATION FUNCTIONS
# ============================================================================

install_wireguard() {
    log_info "Installing WireGuard..."
    case ${PKG_MANAGER} in
        apt-get) apt-get install -y wireguard wireguard-tools qrencode ;;
        dnf) dnf install -y wireguard-tools qrencode; modprobe wireguard 2>/dev/null || true ;;
    esac
    log_success "WireGuard installed"
}

configure_server() {
    echo ""
    echo -e "${GREEN}=== WireGuard Configuration ===${NC}"
    echo ""
    
    SERVER_WG_NIC="wg0"
    
    read -rp "Port [51820]: " SERVER_PORT
    SERVER_PORT=${SERVER_PORT:-51820}
    
    read -rp "IPv4 subnet [10.66.66.0/24]: " SERVER_WG_IPV4
    SERVER_WG_IPV4=${SERVER_WG_IPV4:-10.66.66.0/24}
    SERVER_WG_IPV4_ADDR="${SERVER_WG_IPV4%.*}.1"
    
    check_ipv6
    SERVER_WG_IPV6=""
    if ${IPV6_SUPPORT}; then
        read -rp "IPv6 subnet [fd42:42:42::0/64]: " SERVER_WG_IPV6
        SERVER_WG_IPV6=${SERVER_WG_IPV6:-fd42:42:42::0/64}
        SERVER_WG_IPV6_ADDR="${SERVER_WG_IPV6%::*}::1"
    fi
    
    echo ""
    echo "DNS: 1) Cloudflare  2) Google  3) Quad9  4) Custom"
    read -rp "Select [1]: " DNS_CHOICE
    case ${DNS_CHOICE:-1} in
        2) CLIENT_DNS="8.8.8.8,8.8.4.4" ;;
        3) CLIENT_DNS="9.9.9.9,149.112.112.112" ;;
        4) read -rp "DNS servers: " CLIENT_DNS ;;
        *) CLIENT_DNS="1.1.1.1,1.0.0.1" ;;
    esac
    
    get_server_nic
    
    # Generate keys
    mkdir -p ${WG_DIR}
    chmod 700 ${WG_DIR}
    SERVER_PRIV_KEY=$(wg genkey)
    SERVER_PUB_KEY=$(echo "${SERVER_PRIV_KEY}" | wg pubkey)
    
    # Create config
    cat > ${WG_DIR}/${SERVER_WG_NIC}.conf << EOF
[Interface]
Address = ${SERVER_WG_IPV4_ADDR}/24$(${IPV6_SUPPORT} && echo ",${SERVER_WG_IPV6_ADDR}/64")
ListenPort = ${SERVER_PORT}
PrivateKey = ${SERVER_PRIV_KEY}

PostUp = iptables -I INPUT -p udp --dport ${SERVER_PORT} -j ACCEPT
PostUp = iptables -I FORWARD -i ${SERVER_NIC} -o ${SERVER_WG_NIC} -j ACCEPT
PostUp = iptables -I FORWARD -i ${SERVER_WG_NIC} -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o ${SERVER_NIC} -j MASQUERADE
$(${IPV6_SUPPORT} && echo "PostUp = ip6tables -I FORWARD -i ${SERVER_WG_NIC} -j ACCEPT")
$(${IPV6_SUPPORT} && echo "PostUp = ip6tables -t nat -A POSTROUTING -o ${SERVER_NIC} -j MASQUERADE")

PostDown = iptables -D INPUT -p udp --dport ${SERVER_PORT} -j ACCEPT
PostDown = iptables -D FORWARD -i ${SERVER_NIC} -o ${SERVER_WG_NIC} -j ACCEPT
PostDown = iptables -D FORWARD -i ${SERVER_WG_NIC} -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o ${SERVER_NIC} -j MASQUERADE
$(${IPV6_SUPPORT} && echo "PostDown = ip6tables -D FORWARD -i ${SERVER_WG_NIC} -j ACCEPT")
$(${IPV6_SUPPORT} && echo "PostDown = ip6tables -t nat -D POSTROUTING -o ${SERVER_NIC} -j MASQUERADE")
EOF
    chmod 600 ${WG_DIR}/${SERVER_WG_NIC}.conf
    
    # Save params
    cat > ${WG_PARAMS} << EOF
SERVER_PUB_IP="${PUBLIC_IP}"
SERVER_PUB_NIC="${SERVER_NIC}"
SERVER_WG_NIC="${SERVER_WG_NIC}"
SERVER_WG_IPV4="${SERVER_WG_IPV4}"
SERVER_WG_IPV6="${SERVER_WG_IPV6:-}"
SERVER_PORT="${SERVER_PORT}"
SERVER_PUB_KEY="${SERVER_PUB_KEY}"
CLIENT_DNS="${CLIENT_DNS}"
IPV6_SUPPORT=${IPV6_SUPPORT}
EOF
    chmod 600 ${WG_PARAMS}
    
    log_success "Server configured"
}

start_wireguard() {
    log_info "Starting WireGuard..."
    systemctl enable wg-quick@wg0
    systemctl start wg-quick@wg0
    sleep 2
    service_is_active wg-quick@wg0 && log_success "WireGuard running" || { log_error "Failed to start"; exit 1; }
}

run_install() {
    check_root
    check_os
    get_public_ip
    install_essentials
    install_wireguard
    configure_server
    start_wireguard
    apply_kernel_optimizations
    create_client
    
    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  WireGuard Installation Complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
    echo -e "Server: ${CYAN}${PUBLIC_IP}:${SERVER_PORT}/udp${NC}"
    echo -e "Clients: ${CYAN}${CLIENT_DIR}/${NC}"
    echo ""
}

# ============================================================================
# MANAGEMENT FUNCTIONS
# ============================================================================

load_params() {
    [[ ! -f "${WG_PARAMS}" ]] && { log_error "WireGuard not installed"; exit 1; }
    source "${WG_PARAMS}"
}

get_next_ip() {
    local base="${SERVER_WG_IPV4%.*}"
    local max=1
    while read -r line; do
        [[ "${line}" =~ AllowedIPs.*${base}\.([0-9]+) ]] && [[ ${BASH_REMATCH[1]} -gt ${max} ]] && max=${BASH_REMATCH[1]}
    done < "${WG_DIR}/${SERVER_WG_NIC}.conf"
    echo $((max + 1))
}

create_client() {
    [[ -f "${WG_PARAMS}" ]] && source "${WG_PARAMS}"
    
    echo ""
    read -rp "Client name [client1]: " CLIENT_NAME
    CLIENT_NAME=$(sanitize_client_name "${CLIENT_NAME:-client1}")
    [[ -z "${CLIENT_NAME}" ]] && { log_error "Invalid name"; return 1; }
    [[ -f "${CLIENT_DIR}/${CLIENT_NAME}.conf" ]] && { log_error "Client exists"; return 1; }
    
    mkdir -p "${CLIENT_DIR}"
    
    CLIENT_PRIV_KEY=$(wg genkey)
    CLIENT_PUB_KEY=$(echo "${CLIENT_PRIV_KEY}" | wg pubkey)
    CLIENT_PSK=$(wg genpsk)
    
    local next_ip
    next_ip=$(get_next_ip)
    CLIENT_IPV4="${SERVER_WG_IPV4%.*}.${next_ip}"
    [[ -n "${SERVER_WG_IPV6}" ]] && CLIENT_IPV6="${SERVER_WG_IPV6%::*}::${next_ip}"
    
    # Add to server
    cat >> ${WG_DIR}/${SERVER_WG_NIC}.conf << EOF

### Client: ${CLIENT_NAME}
[Peer]
PublicKey = ${CLIENT_PUB_KEY}
PresharedKey = ${CLIENT_PSK}
AllowedIPs = ${CLIENT_IPV4}/32$([[ -n "${SERVER_WG_IPV6}" ]] && echo ",${CLIENT_IPV6}/128")
EOF

    echo ""
    echo -e "${CYAN}Server address for client connections:${NC}"
    echo -e "  Can be: domain (vpn.example.com), DDNS (myvpn.ddns.net), or IP"
    read -rp "Server address [${SERVER_PUB_IP:-$PUBLIC_IP}]: " CLIENT_ENDPOINT
    CLIENT_ENDPOINT=${CLIENT_ENDPOINT:-${SERVER_PUB_IP:-$PUBLIC_IP}}
    
    echo ""
    echo -e "${YELLOW}AmneziaWG 1.5 adds obfuscation for censorship bypass.${NC}"
    echo -e "${YELLOW}NOTE: Requires AmneziaVPN 4.8.2.1+ or AmneziaWG 1.5+ client!${NC}"
    read -rp "Enable AmneziaWG 1.5? [y/N]: " AWG
    
    if [[ "${AWG}" =~ ^[Yy]$ ]]; then
        cat > ${CLIENT_DIR}/${CLIENT_NAME}.conf << EOF
[Interface]
PrivateKey = ${CLIENT_PRIV_KEY}
Address = ${CLIENT_IPV4}/32$([[ -n "${SERVER_WG_IPV6}" ]] && echo ",${CLIENT_IPV6}/128")
DNS = ${CLIENT_DNS}
Jc = ${AWG_JC}
Jmin = ${AWG_JMIN}
Jmax = ${AWG_JMAX}
I1 = ${AWG_I1}
I2 = ${AWG_I2}
I3 = ${AWG_I3}
I4 = ${AWG_I4}
I5 = ${AWG_I5}
MTU = ${AWG_MTU}

[Peer]
PublicKey = ${SERVER_PUB_KEY}
PresharedKey = ${CLIENT_PSK}
Endpoint = ${CLIENT_ENDPOINT}:${SERVER_PORT}
AllowedIPs = 0.0.0.0/0$([[ -n "${SERVER_WG_IPV6}" ]] && echo ",::/0")
PersistentKeepalive = 15
EOF
        AWG_ENABLED=true
    else
        cat > ${CLIENT_DIR}/${CLIENT_NAME}.conf << EOF
[Interface]
PrivateKey = ${CLIENT_PRIV_KEY}
Address = ${CLIENT_IPV4}/32$([[ -n "${SERVER_WG_IPV6}" ]] && echo ",${CLIENT_IPV6}/128")
DNS = ${CLIENT_DNS}

[Peer]
PublicKey = ${SERVER_PUB_KEY}
PresharedKey = ${CLIENT_PSK}
Endpoint = ${CLIENT_ENDPOINT}:${SERVER_PORT}
AllowedIPs = 0.0.0.0/0$([[ -n "${SERVER_WG_IPV6}" ]] && echo ",::/0")
PersistentKeepalive = 25
EOF
        AWG_ENABLED=false
    fi
    
    chmod 600 ${CLIENT_DIR}/${CLIENT_NAME}.conf
    wg syncconf ${SERVER_WG_NIC} <(wg-quick strip ${SERVER_WG_NIC}) 2>/dev/null || true
    
    log_success "Client '${CLIENT_NAME}' created"
    echo ""
    cat ${CLIENT_DIR}/${CLIENT_NAME}.conf
    echo ""
    
    # Show QR code only for standard WireGuard (not AmneziaWG)
    if [[ "${AWG_ENABLED}" != "true" ]] && command -v qrencode &>/dev/null; then
        echo -e "${CYAN}QR Code:${NC}"
        qrencode -t ansiutf8 < ${CLIENT_DIR}/${CLIENT_NAME}.conf
    fi
}

remove_client() {
    load_params
    get_client_name || return 1
    
    grep -q "### Client: ${CLIENT_NAME}" "${WG_DIR}/${SERVER_WG_NIC}.conf" || { log_error "Not found"; return 1; }
    
    confirm_action "Remove client '${CLIENT_NAME}'?" || return 0
    
    sed -i "/### Client: ${CLIENT_NAME}/,/^$/d" "${WG_DIR}/${SERVER_WG_NIC}.conf"
    rm -f "${CLIENT_DIR}/${CLIENT_NAME}.conf"
    wg syncconf ${SERVER_WG_NIC} <(wg-quick strip ${SERVER_WG_NIC})
    
    log_success "Client '${CLIENT_NAME}' removed"
    return 0
}

list_clients() {
    load_params
    echo ""
    echo -e "${GREEN}=== WireGuard Clients ===${NC}"
    CLIENT_LIST=()
    while read -r line; do
        if [[ "${line}" =~ ^###\ Client:\ (.+) ]]; then
            CLIENT_LIST+=("${BASH_REMATCH[1]}")
        fi
    done < "${WG_DIR}/${SERVER_WG_NIC}.conf"
    
    if [[ ${#CLIENT_LIST[@]} -eq 0 ]]; then
        echo "  No clients"
    else
        for i in "${!CLIENT_LIST[@]}"; do
            echo "  $((i+1))) ${CLIENT_LIST[$i]}"
        done
    fi
    echo ""
    return 0
}

get_client_name() {
    load_params
    list_clients
    [[ ${#CLIENT_LIST[@]} -eq 0 ]] && return 1
    
    read -rp "Client (name or number): " input
    
    # Check if input is a number
    if [[ "${input}" =~ ^[0-9]+$ ]]; then
        local idx
        idx=$((input - 1))
        if [[ ${idx} -ge 0 && ${idx} -lt ${#CLIENT_LIST[@]} ]]; then
            CLIENT_NAME="${CLIENT_LIST[$idx]}"
        else
            log_error "Invalid number"
            return 1
        fi
    else
        CLIENT_NAME="${input}"
    fi
    return 0
}

show_client() {
    load_params
    get_client_name || return 1
    [[ -f "${CLIENT_DIR}/${CLIENT_NAME}.conf" ]] || { log_error "Config not found"; return 1; }
    echo ""
    cat "${CLIENT_DIR}/${CLIENT_NAME}.conf"
    echo ""
    # Show QR if not AmneziaWG and qrencode available
    if ! grep -q "^Jc = " "${CLIENT_DIR}/${CLIENT_NAME}.conf" 2>/dev/null; then
        command -v qrencode &>/dev/null && qrencode -t ansiutf8 < "${CLIENT_DIR}/${CLIENT_NAME}.conf"
    fi
    return 0
}

show_status() {
    load_params
    echo ""
    echo -e "${GREEN}=== WireGuard Status ===${NC}"
    echo ""
    wg show
    echo ""
    systemctl status wg-quick@${SERVER_WG_NIC} --no-pager | head -10
}

uninstall_wireguard() {
    log_warning "This will remove WireGuard and all configurations!"
    confirm_action "Continue?" || return
    
    systemctl stop wg-quick@wg0 2>/dev/null || true
    systemctl disable wg-quick@wg0 2>/dev/null || true
    rm -rf /etc/wireguard /etc/vpn/wireguard
    
    log_success "WireGuard uninstalled"
}

# ============================================================================
# MAIN MENU
# ============================================================================

main_menu() {
    check_root
    
    while true; do
        echo ""
        echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║         WireGuard VPN                  ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
        echo ""
        
        if [[ -f "${WG_PARAMS}" ]]; then
            source "${WG_PARAMS}"
            service_is_active "wg-quick@${SERVER_WG_NIC}" && echo -e "  Status: ${GREEN}Running${NC}" || echo -e "  Status: ${RED}Stopped${NC}"
            echo ""
            echo "  1) Add client"
            echo "  2) Remove client"
            echo "  3) Show client config"
            echo "  4) List clients"
            echo "  5) Show status"
            echo "  6) Restart WireGuard"
            echo "  7) Uninstall"
        else
            echo -e "  Status: ${YELLOW}Not installed${NC}"
            echo ""
            echo "  1) Install WireGuard"
        fi
        echo ""
        echo "  0) Exit"
        echo ""
        read -rp "Select: " choice
        
        if [[ -f ${WG_PARAMS} ]]; then
            case ${choice} in
                1) create_client; press_enter ;;
                2) remove_client; press_enter ;;
                3) show_client; press_enter ;;
                4) list_clients; press_enter ;;
                5) show_status; press_enter ;;
                6) systemctl restart wg-quick@${SERVER_WG_NIC}; log_success "Restarted"; press_enter ;;
                7) uninstall_wireguard ;;
                0) exit 0 ;;
            esac
        else
            case ${choice} in
                1) run_install; press_enter ;;
                0) exit 0 ;;
            esac
        fi
    done
}

# Run
main_menu
