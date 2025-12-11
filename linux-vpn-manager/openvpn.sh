#!/bin/bash
#
# OpenVPN - Install & Manage (Combined)
#

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" || { echo "ERROR: common.sh not found"; exit 1; }

OVPN_DIR="/etc/openvpn"
EASYRSA_DIR="${OVPN_DIR}/easy-rsa"
OVPN_PARAMS="${OVPN_DIR}/params"
CLIENT_DIR="/etc/vpn/openvpn/clients"

# ============================================================================
# INSTALLATION FUNCTIONS
# ============================================================================

install_openvpn() {
    log_info "Installing OpenVPN..."
    case ${PKG_MANAGER} in
        apt-get) apt-get install -y openvpn easy-rsa iptables ;;
        dnf) dnf install -y epel-release; dnf install -y openvpn easy-rsa iptables ;;
    esac
    log_success "OpenVPN installed"
}

setup_pki() {
    log_info "Setting up PKI..."
    
    mkdir -p ${EASYRSA_DIR}
    
    if [[ -d /usr/share/easy-rsa/3 ]]; then
        cp -r /usr/share/easy-rsa/3/* ${EASYRSA_DIR}/
    elif [[ -d /usr/share/easy-rsa ]]; then
        cp -r /usr/share/easy-rsa/* ${EASYRSA_DIR}/
    fi
    
    cd ${EASYRSA_DIR}
    ./easyrsa --batch init-pki
    ./easyrsa --batch --req-cn="VPN-CA" build-ca nopass
    ./easyrsa --batch build-server-full server nopass
    ./easyrsa --batch gen-dh
    openvpn --genkey secret ${OVPN_DIR}/ta.key
    
    log_success "PKI setup complete"
}

configure_server() {
    echo ""
    echo -e "${GREEN}=== OpenVPN Configuration ===${NC}"
    echo ""
    
    read -rp "Port [1194]: " OVPN_PORT
    OVPN_PORT=${OVPN_PORT:-1194}
    
    echo "Protocol: 1) UDP (recommended)  2) TCP"
    read -rp "Select [1]: " proto_choice
    [[ "${proto_choice}" == "2" ]] && OVPN_PROTO="tcp" || OVPN_PROTO="udp"
    
    get_server_nic
    
    cat > ${OVPN_DIR}/server.conf << EOF
port ${OVPN_PORT}
proto ${OVPN_PROTO}
dev tun
ca ${EASYRSA_DIR}/pki/ca.crt
cert ${EASYRSA_DIR}/pki/issued/server.crt
key ${EASYRSA_DIR}/pki/private/server.key
dh ${EASYRSA_DIR}/pki/dh.pem
tls-auth ${OVPN_DIR}/ta.key 0
server 10.8.0.0 255.255.255.0
push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS 1.1.1.1"
push "dhcp-option DNS 8.8.8.8"
keepalive 10 120
cipher AES-256-GCM
auth SHA256
user nobody
group ${GROUP_NAME}
persist-key
persist-tun
status /var/log/openvpn-status.log
log-append /var/log/openvpn.log
verb 3
explicit-exit-notify 1
EOF

    cat > ${OVPN_PARAMS} << EOF
OVPN_PORT=${OVPN_PORT}
OVPN_PROTO=${OVPN_PROTO}
PUBLIC_IP="${PUBLIC_IP}"
SERVER_NIC="${SERVER_NIC}"
EOF
    chmod 600 ${OVPN_PARAMS}
    
    # NAT rules
    iptables -t nat -A POSTROUTING -s 10.8.0.0/24 -o ${SERVER_NIC} -j MASQUERADE
    iptables -A INPUT -p ${OVPN_PROTO} --dport ${OVPN_PORT} -j ACCEPT
    iptables -A FORWARD -s 10.8.0.0/24 -j ACCEPT
    save_iptables_rules
    
    log_success "Server configured"
}

start_openvpn() {
    log_info "Starting OpenVPN..."
    systemctl enable openvpn-server@server
    systemctl start openvpn-server@server
    sleep 2
    service_is_active openvpn-server@server && log_success "OpenVPN running" || { log_error "Failed to start"; exit 1; }
}

create_client() {
    [[ -f "${OVPN_PARAMS}" ]] && source "${OVPN_PARAMS}"
    
    echo ""
    read -rp "Client name [client1]: " CLIENT_NAME
    CLIENT_NAME=$(sanitize_client_name "${CLIENT_NAME:-client1}")
    [[ -z "${CLIENT_NAME}" ]] && { log_error "Invalid name"; return 1; }
    
    mkdir -p "${CLIENT_DIR}"
    
    cd "${EASYRSA_DIR}"
    ./easyrsa --batch build-client-full "${CLIENT_NAME}" nopass
    
    cat > ${CLIENT_DIR}/${CLIENT_NAME}.ovpn << EOF
client
dev tun
proto ${OVPN_PROTO}
remote ${PUBLIC_IP} ${OVPN_PORT}
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
cipher AES-256-GCM
auth SHA256
key-direction 1
verb 3

<ca>
$(cat ${EASYRSA_DIR}/pki/ca.crt)
</ca>

<cert>
$(sed -n '/BEGIN CERTIFICATE/,/END CERTIFICATE/p' ${EASYRSA_DIR}/pki/issued/${CLIENT_NAME}.crt)
</cert>

<key>
$(cat ${EASYRSA_DIR}/pki/private/${CLIENT_NAME}.key)
</key>

<tls-auth>
$(cat ${OVPN_DIR}/ta.key)
</tls-auth>
EOF

    chmod 600 ${CLIENT_DIR}/${CLIENT_NAME}.ovpn
    log_success "Client '${CLIENT_NAME}' created: ${CLIENT_DIR}/${CLIENT_NAME}.ovpn"
}

run_install() {
    check_root
    check_os
    get_public_ip
    install_essentials
    install_openvpn
    setup_pki
    configure_server
    start_openvpn
    apply_kernel_optimizations
    create_client
    
    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  OpenVPN Installation Complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
}

# ============================================================================
# MANAGEMENT FUNCTIONS
# ============================================================================

load_params() {
    [[ ! -f "${OVPN_PARAMS}" ]] && { log_error "OpenVPN not installed"; exit 1; }
    source "${OVPN_PARAMS}"
}

list_clients() {
    echo ""
    echo -e "${GREEN}=== OpenVPN Clients ===${NC}"
    local count=0
    local name
    for cert in ${EASYRSA_DIR}/pki/issued/*.crt; do
        [[ -f "$cert" ]] || continue
        name=$(basename "$cert" .crt)
        [[ "$name" == "server" ]] && continue
        count=$((count + 1))
        echo "  ${count}) ${name}"
    done
    if [[ ${count} -eq 0 ]]; then
        echo "  No clients"
    fi
    echo ""
    return 0
}

remove_client() {
    load_params
    list_clients
    read -rp "Client to remove: " CLIENT_NAME
    
    [[ ! -f "${EASYRSA_DIR}/pki/issued/${CLIENT_NAME}.crt" ]] && { log_error "Not found"; return 1; }
    
    cd ${EASYRSA_DIR}
    ./easyrsa --batch revoke "${CLIENT_NAME}" 2>/dev/null || true
    ./easyrsa --batch gen-crl 2>/dev/null || true
    rm -f "${CLIENT_DIR}/${CLIENT_NAME}.ovpn"
    
    log_success "Client '${CLIENT_NAME}' removed"
}

show_status() {
    echo ""
    echo -e "${GREEN}=== OpenVPN Status ===${NC}"
    echo ""
    systemctl status openvpn-server@server --no-pager | head -15
}

uninstall_openvpn() {
    log_warning "This will remove OpenVPN and all configurations!"
    confirm_action "Continue?" || return
    
    systemctl stop openvpn-server@server 2>/dev/null || true
    systemctl disable openvpn-server@server 2>/dev/null || true
    rm -rf /etc/openvpn /etc/vpn/openvpn
    
    log_success "OpenVPN uninstalled"
}

# ============================================================================
# MAIN MENU
# ============================================================================

main_menu() {
    check_root
    check_os
    
    while true; do
        echo ""
        echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║         OpenVPN Server                 ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
        echo ""
        
        if [[ -f ${OVPN_PARAMS} ]]; then
            service_is_active openvpn-server@server && echo -e "  Status: ${GREEN}Running${NC}" || echo -e "  Status: ${RED}Stopped${NC}"
            echo ""
            echo "  1) Add client"
            echo "  2) Remove client"
            echo "  3) List clients"
            echo "  4) Show status"
            echo "  5) Restart service"
            echo "  6) Uninstall"
        else
            echo -e "  Status: ${YELLOW}Not installed${NC}"
            echo ""
            echo "  1) Install OpenVPN"
        fi
        echo ""
        echo "  0) Exit"
        echo ""
        read -rp "Select: " choice
        
        if [[ -f ${OVPN_PARAMS} ]]; then
            case ${choice} in
                1) create_client ;;
                2) remove_client ;;
                3) list_clients ;;
                4) show_status ;;
                5) systemctl restart openvpn-server@server; log_success "Restarted" ;;
                6) uninstall_openvpn ;;
                0) exit 0 ;;
            esac
        else
            case ${choice} in
                1) run_install ;;
                0) exit 0 ;;
            esac
        fi
    done
}

main_menu
