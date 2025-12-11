#!/bin/bash
#
# Shadowsocks VPN - Install & Manage (Combined)
#

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" || { echo "ERROR: common.sh not found"; exit 1; }

SS_DIR="/etc/shadowsocks"
SS_CONFIG="${SS_DIR}/config.json"
SS_PARAMS="${SS_DIR}/params"
CLIENT_DIR="/etc/vpn/shadowsocks/clients"
SS_VERSION="v1.21.2"

# ============================================================================
# INSTALLATION FUNCTIONS
# ============================================================================

install_shadowsocks() {
    log_info "Installing shadowsocks-rust ${SS_VERSION}..."
    
    local ARCH DOWNLOAD_URL
    ARCH=$(uname -m)
    DOWNLOAD_URL=""
    case ${ARCH} in
        x86_64) DOWNLOAD_URL="https://github.com/shadowsocks/shadowsocks-rust/releases/download/${SS_VERSION}/shadowsocks-${SS_VERSION}.x86_64-unknown-linux-gnu.tar.xz" ;;
        aarch64) DOWNLOAD_URL="https://github.com/shadowsocks/shadowsocks-rust/releases/download/${SS_VERSION}/shadowsocks-${SS_VERSION}.aarch64-unknown-linux-gnu.tar.xz" ;;
        *) log_error "Unsupported architecture: ${ARCH}"; exit 1 ;;
    esac
    
    cd /tmp
    rm -rf ss-download && mkdir ss-download && cd ss-download
    wget -q --show-progress -O shadowsocks.tar.xz "${DOWNLOAD_URL}"
    tar -xf shadowsocks.tar.xz
    
    for bin in ssserver sslocal ssurl; do
        [[ -f ${bin} ]] && install -m 755 ${bin} /usr/bin/
    done
    
    cd /tmp && rm -rf ss-download
    command -v ssserver &>/dev/null && log_success "shadowsocks-rust installed" || { log_error "Installation failed"; exit 1; }
}

create_shadowsocks_user() {
    # Create dedicated user for Shadowsocks (fixes Ubuntu 24.04 warning about 'nobody')
    if ! id "shadowsocks" &>/dev/null; then
        log_info "Creating shadowsocks system user..."
        useradd --system --no-create-home --shell /usr/sbin/nologin shadowsocks
        log_success "User 'shadowsocks' created"
    fi
}

configure_server() {
    echo ""
    echo -e "${GREEN}=== Shadowsocks Configuration ===${NC}"
    echo ""
    
    create_shadowsocks_user
    
    mkdir -p ${SS_DIR} ${CLIENT_DIR}
    chown shadowsocks:${GROUP_NAME} ${SS_DIR}
    chmod 700 ${SS_DIR}
    
    read -rp "Port [8388]: " SS_PORT
    SS_PORT=${SS_PORT:-8388}
    
    SS_PASSWORD=$(generate_password 32)
    echo -e "Generated password: ${CYAN}${SS_PASSWORD}${NC}"
    read -rp "Use this password? [Y/n]: " use_pass
    [[ "${use_pass}" =~ ^[Nn]$ ]] && read -rp "Enter password: " SS_PASSWORD
    
    echo ""
    echo "Encryption: 1) chacha20-ietf-poly1305  2) aes-256-gcm  3) aes-128-gcm"
    read -rp "Select [1]: " method_choice
    case ${method_choice} in
        2) SS_METHOD="aes-256-gcm" ;;
        3) SS_METHOD="aes-128-gcm" ;;
        *) SS_METHOD="chacha20-ietf-poly1305" ;;
    esac
    
    cat > ${SS_CONFIG} << EOF
{
    "server": "0.0.0.0",
    "server_port": ${SS_PORT},
    "password": "${SS_PASSWORD}",
    "method": "${SS_METHOD}",
    "timeout": 300,
    "mode": "tcp_and_udp",
    "fast_open": true,
    "no_delay": true
}
EOF

    chown shadowsocks:${GROUP_NAME} ${SS_CONFIG}
    chmod 600 ${SS_CONFIG}
    
    cat > ${SS_PARAMS} << EOF
SS_PORT=${SS_PORT}
SS_PASSWORD="${SS_PASSWORD}"
SS_METHOD="${SS_METHOD}"
PUBLIC_IP="${PUBLIC_IP}"
EOF
    chmod 600 ${SS_PARAMS}
    
    log_success "Configuration saved"
}

create_service() {
    log_info "Creating systemd service..."
    
    cat > /etc/systemd/system/shadowsocks.service << EOF
[Unit]
Description=Shadowsocks Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=shadowsocks
Group=${GROUP_NAME}
LimitNOFILE=32768
ExecStart=/usr/bin/ssserver -c ${SS_CONFIG}
Restart=always
RestartSec=3
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable shadowsocks
}

start_shadowsocks() {
    log_info "Starting Shadowsocks..."
    systemctl start shadowsocks
    sleep 2
    service_is_active shadowsocks && log_success "Shadowsocks running" || { log_error "Failed to start"; journalctl -u shadowsocks --no-pager -n 10; exit 1; }
}

generate_client_config() {
    source "${SS_PARAMS}"
    
    local SS_URL
    SS_URL="ss://$(echo -n "${SS_METHOD}:${SS_PASSWORD}" | base64 -w0)@${PUBLIC_IP}:${SS_PORT}"
    
    cat > "${CLIENT_DIR}/client-config.json" << EOF
{
    "server": "${PUBLIC_IP}",
    "server_port": ${SS_PORT},
    "password": "${SS_PASSWORD}",
    "method": "${SS_METHOD}",
    "timeout": 300,
    "mode": "tcp_and_udp"
}
EOF

    echo "${SS_URL}" > ${CLIENT_DIR}/ss-url.txt
    
    echo ""
    echo -e "${GREEN}=== Client Configuration ===${NC}"
    echo ""
    echo -e "Server: ${CYAN}${PUBLIC_IP}${NC}"
    echo -e "Port: ${CYAN}${SS_PORT}${NC}"
    echo -e "Password: ${CYAN}${SS_PASSWORD}${NC}"
    echo -e "Method: ${CYAN}${SS_METHOD}${NC}"
    echo ""
    echo -e "SS URL: ${CYAN}${SS_URL}${NC}"
    echo ""
    
    command -v qrencode &>/dev/null && {
        echo -e "${CYAN}QR Code:${NC}"
        echo "${SS_URL}" | qrencode -t ansiutf8
    }
}

run_install() {
    check_root
    check_os
    get_public_ip
    install_essentials
    install_shadowsocks
    configure_server
    create_service
    firewall_open_port ${SS_PORT} tcp
    firewall_open_port ${SS_PORT} udp
    start_shadowsocks
    generate_client_config
    
    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Shadowsocks Installation Complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
}

# ============================================================================
# MANAGEMENT FUNCTIONS
# ============================================================================

load_params() {
    [[ ! -f "${SS_PARAMS}" ]] && { log_error "Shadowsocks not installed"; exit 1; }
    source "${SS_PARAMS}"
}

show_config() {
    load_params
    generate_client_config
}

change_password() {
    load_params
    
    local NEW_PASS
    NEW_PASS=$(generate_password 32)
    echo -e "New password: ${CYAN}${NEW_PASS}${NC}"
    read -rp "Use this? [Y/n]: " use_pass
    [[ "${use_pass}" =~ ^[Nn]$ ]] && read -rp "Enter password: " NEW_PASS
    
    local tmp
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' RETURN
    
    jq --arg pw "${NEW_PASS}" '.password = $pw' "${SS_CONFIG}" > "$tmp" && mv "$tmp" "${SS_CONFIG}"
    chown shadowsocks:"${GROUP_NAME}" "${SS_CONFIG}"
    chmod 600 "${SS_CONFIG}"
    
    sed -i "s|^SS_PASSWORD=.*|SS_PASSWORD=\"${NEW_PASS}\"|" "${SS_PARAMS}"
    SS_PASSWORD="${NEW_PASS}"
    
    systemctl restart shadowsocks
    log_success "Password changed"
    generate_client_config
}

change_port() {
    load_params
    
    local NEW_PORT
    read -rp "New port [${SS_PORT}]: " NEW_PORT
    NEW_PORT=${NEW_PORT:-$SS_PORT}
    
    if ! validate_port "${NEW_PORT}"; then
        log_error "Invalid port. Must be 1-65535"
        return 1
    fi
    
    local tmp
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' RETURN
    
    jq --argjson port "${NEW_PORT}" '.server_port = $port' "${SS_CONFIG}" > "$tmp" && mv "$tmp" "${SS_CONFIG}"
    chown shadowsocks:"${GROUP_NAME}" "${SS_CONFIG}"
    chmod 600 "${SS_CONFIG}"
    
    sed -i "s|^SS_PORT=.*|SS_PORT=${NEW_PORT}|" "${SS_PARAMS}"
    
    firewall_open_port "${NEW_PORT}" tcp
    firewall_open_port "${NEW_PORT}" udp
    
    systemctl restart shadowsocks
    log_success "Port changed to ${NEW_PORT}"
}

show_status() {
    echo ""
    echo -e "${GREEN}=== Shadowsocks Status ===${NC}"
    echo ""
    systemctl status shadowsocks --no-pager | head -15
}

uninstall_shadowsocks() {
    log_warning "This will remove Shadowsocks and all configurations!"
    confirm_action "Continue?" || return
    
    systemctl stop shadowsocks 2>/dev/null || true
    systemctl disable shadowsocks 2>/dev/null || true
    rm -rf /etc/shadowsocks /etc/vpn/shadowsocks
    rm -f /etc/systemd/system/shadowsocks.service
    rm -f /usr/bin/ssserver /usr/bin/sslocal /usr/bin/ssurl
    systemctl daemon-reload
    
    log_success "Shadowsocks uninstalled"
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
        echo -e "${GREEN}║         Shadowsocks Proxy              ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
        echo ""
        
        if [[ -f ${SS_PARAMS} ]]; then
            service_is_active shadowsocks && echo -e "  Status: ${GREEN}Running${NC}" || echo -e "  Status: ${RED}Stopped${NC}"
            echo ""
            echo "  1) Show config & QR"
            echo "  2) Change password"
            echo "  3) Change port"
            echo "  4) Show status"
            echo "  5) Restart service"
            echo "  6) Uninstall"
        else
            echo -e "  Status: ${YELLOW}Not installed${NC}"
            echo ""
            echo "  1) Install Shadowsocks"
        fi
        echo ""
        echo "  0) Exit"
        echo ""
        read -rp "Select: " choice
        
        if [[ -f ${SS_PARAMS} ]]; then
            case ${choice} in
                1) show_config ;;
                2) change_password ;;
                3) change_port ;;
                4) show_status ;;
                5) systemctl restart shadowsocks; log_success "Restarted" ;;
                6) uninstall_shadowsocks ;;
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
