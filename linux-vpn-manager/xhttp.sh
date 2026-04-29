#!/bin/bash
#
# XRay CDN Tunnel - Install & Manage
#
# VLESS+gRPC transport through Cloudflare Tunnel.
# Traffic is indistinguishable from normal HTTPS website visits.
# Designed for heavily censored networks (Russia TSPU, etc.)
#
# Compatible with AmneziaVPN, v2rayNG, Hiddify, v2rayN
#
# Prerequisites (one-time, in Cloudflare dashboard):
#   1. Add domain to Cloudflare (free plan), change NS at registrar
#   2. Enable gRPC: domain > Network > gRPC ON
#   3. SSL mode: domain > SSL/TLS > Full
#   4. Disable Browser Integrity Check: domain > Security > Settings
#   5. Create tunnel: Zero Trust > Networking > Tunnels > Cloudflared
#   6. Set tunnel route via CF API with http2Origin + noTLSVerify
#      (dashboard UI doesn't expose these settings)
#

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" || { echo "ERROR: common.sh not found"; exit 1; }

XHTTP_DIR="/usr/local/etc/xray-xhttp"
XHTTP_CONFIG="${XHTTP_DIR}/config.json"
XHTTP_PARAMS="${XHTTP_DIR}/params"
CLIENT_DIR="/etc/vpn/xhttp/clients"

# Defaults
DEFAULT_PORT=10443
DEFAULT_SERVICE_NAME="xh"
DEFAULT_FINGERPRINT="chrome"


# ============================================================================
# INSTALLATION FUNCTIONS
# ============================================================================

install_xray_binary() {
    if command -v xray &>/dev/null || [[ -x /usr/local/bin/xray ]]; then
        export PATH="/usr/local/bin:$PATH"
        log_success "XRay already installed: $(xray version | head -1)"
        return 0
    fi

    log_info "Installing XRay via official script..."
    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install || true

    if ! command -v xray &>/dev/null && [[ ! -x /usr/local/bin/xray ]]; then
        log_error "XRay installation failed - binary not found"
        exit 1
    fi

    export PATH="/usr/local/bin:$PATH"
    log_success "XRay installed: $(xray version | head -1)"
}

install_cloudflared() {
    if command -v cloudflared &>/dev/null; then
        log_success "cloudflared already installed: $(cloudflared version 2>&1 | head -1)"
        return 0
    fi

    log_info "Installing cloudflared..."

    # Try apt first (Debian/Ubuntu)
    if [[ "${PKG_MANAGER}" == "apt-get" ]]; then
        mkdir -p /usr/share/keyrings
        chmod 0755 /usr/share/keyrings
        curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
        echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs 2>/dev/null || echo stable) main" | tee /etc/apt/sources.list.d/cloudflared.list
        apt-get update -qq && apt-get install -y cloudflared
    else
        # Binary install for RHEL/Rocky/AlmaLinux
        local arch
        arch=$(uname -m)
        case "${arch}" in
            x86_64)  arch="amd64" ;;
            aarch64) arch="arm64" ;;
            armv7l)  arch="arm" ;;
            *)       log_error "Unsupported architecture: ${arch}"; exit 1 ;;
        esac
        curl -fsSL -o /usr/local/bin/cloudflared \
            "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}"
        chmod +x /usr/local/bin/cloudflared
    fi

    if ! command -v cloudflared &>/dev/null; then
        log_error "cloudflared installation failed"
        exit 1
    fi

    log_success "cloudflared installed: $(cloudflared version 2>&1 | head -1)"
}

generate_self_signed_cert() {
    log_info "Generating self-signed TLS certificate..."
    openssl req -x509 -newkey rsa:2048 \
        -keyout "${XHTTP_DIR}/key.pem" \
        -out "${XHTTP_DIR}/cert.pem" \
        -days 3650 -nodes \
        -subj "/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null
    chmod 600 "${XHTTP_DIR}/key.pem" "${XHTTP_DIR}/cert.pem"
    log_success "TLS certificate generated (valid 10 years)"
}

create_xhttp_user() {
    if ! id "xray" &>/dev/null; then
        log_info "Creating xray system user..."
        useradd --system --no-create-home --shell /usr/sbin/nologin xray
        log_success "User 'xray' created"
    fi
}

create_xhttp_service() {
    log_info "Creating xray-xhttp systemd service..."

    systemctl stop xray-xhttp 2>/dev/null || true
    systemctl disable xray-xhttp 2>/dev/null || true

    mkdir -p "${XHTTP_DIR}" "${CLIENT_DIR}"
    chown -R xray:"${GROUP_NAME}" "${XHTTP_DIR}" 2>/dev/null || true
    chown -R root:root "${CLIENT_DIR}" 2>/dev/null || true
    chmod 700 "${XHTTP_DIR}" "${CLIENT_DIR}"

    cat > /etc/systemd/system/xray-xhttp.service << EOF
[Unit]
Description=XRay CDN Tunnel (gRPC) Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xray
Group=${GROUP_NAME}
LimitNOFILE=32768
ExecStart=/usr/local/bin/xray run -config ${XHTTP_CONFIG}
Restart=always
RestartSec=3
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    log_success "Service xray-xhttp created"
}

create_cloudflared_service() {
    local token="$1"

    log_info "Creating cloudflared systemd service..."

    systemctl stop cloudflared 2>/dev/null || true
    cloudflared service uninstall 2>/dev/null || true

    # Critical: --protocol http2 prevents gRPC "context canceled" errors
    # that occur with the default QUIC protocol
    cat > /etc/systemd/system/cloudflared.service << EOF
[Unit]
Description=cloudflared tunnel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/cloudflared --no-autoupdate --protocol http2 tunnel run --token ${token}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable cloudflared
    systemctl start cloudflared
    sleep 3

    if service_is_active cloudflared; then
        log_success "Cloudflare Tunnel service started"
    else
        log_error "Cloudflare Tunnel failed to start"
        journalctl -u cloudflared --no-pager -n 10
        exit 1
    fi
}

configure_server() {
    echo ""
    echo -e "${GREEN}=== XRay CDN Tunnel (gRPC) Configuration ===${NC}"
    echo ""

    mkdir -p "${XHTTP_DIR}" "${CLIENT_DIR}"
    chown xray:"${GROUP_NAME}" "${XHTTP_DIR}" 2>/dev/null || chown xray:nogroup "${XHTTP_DIR}"
    chown root:root "${CLIENT_DIR}"
    chmod 700 "${XHTTP_DIR}" "${CLIENT_DIR}"

    # Cloudflare domain
    echo -e "${CYAN}Cloudflare domain (the public hostname for this tunnel):${NC}"
    echo -e "  Example: logs.example.com"
    read -rp "Domain: " CF_DOMAIN
    [[ -z "${CF_DOMAIN}" ]] && { log_error "Domain required"; exit 1; }

    # Cloudflare tunnel token
    echo ""
    echo -e "${CYAN}Cloudflare Tunnel token:${NC}"
    echo -e "  Get from: CF dashboard > Zero Trust > Networking > Tunnels > your tunnel"
    echo -e "  Copy the token string from the install command"
    read -rp "Token: " CF_TOKEN
    [[ -z "${CF_TOKEN}" ]] && { log_error "Token required"; exit 1; }

    # gRPC service name
    echo ""
    read -rp "gRPC service name [${DEFAULT_SERVICE_NAME}]: " SERVICE_NAME
    SERVICE_NAME=${SERVICE_NAME:-${DEFAULT_SERVICE_NAME}}

    read -rp "Local listen port [${DEFAULT_PORT}]: " XHTTP_PORT
    XHTTP_PORT=${XHTTP_PORT:-${DEFAULT_PORT}}

    # Save params
    cat > "${XHTTP_PARAMS}" << EOF
CF_DOMAIN='${CF_DOMAIN}'
SERVICE_NAME='${SERVICE_NAME}'
XHTTP_PORT=${XHTTP_PORT}
XHTTP_FINGERPRINT='${DEFAULT_FINGERPRINT}'
EOF
    chmod 600 "${XHTTP_PARAMS}"
    chown xray:"${GROUP_NAME}" "${XHTTP_PARAMS}" 2>/dev/null || chown xray:nogroup "${XHTTP_PARAMS}"

    # Generate self-signed cert for gRPC TLS (required for HTTP/2 ALPN)
    generate_self_signed_cert
    chown xray:"${GROUP_NAME}" "${XHTTP_DIR}/key.pem" "${XHTTP_DIR}/cert.pem" 2>/dev/null || true

    # Install cloudflared with --protocol http2
    create_cloudflared_service "${CF_TOKEN}"

    # Create initial Xray config (no clients yet)
    create_xhttp_config

    log_success "Server configured"

    echo ""
    echo -e "${YELLOW}IMPORTANT - Configure tunnel origin settings:${NC}"
    echo ""
    echo -e "  Go to Zero Trust dashboard (one.dash.cloudflare.com):"
    echo -e "  ${CYAN}Networks > Connectors > your tunnel > Published application routes${NC}"
    echo -e "  Edit the route, expand ${CYAN}Additional application settings${NC}:"
    echo -e "    - Service URL: ${CYAN}https://localhost:${XHTTP_PORT}${NC}"
    echo -e "    - TLS > No TLS Verify: ${CYAN}ON${NC}"
    echo -e "    - TLS > HTTP2 connection: ${CYAN}ON${NC}"
    echo ""
    echo -e "  Note: These settings are only in the Zero Trust dashboard,"
    echo -e "  NOT in the main CF dashboard 'Networking > Tunnels' UI."
    echo ""
    echo -e "  Also ensure DNS has a CNAME record:"
    echo -e "    ${CYAN}$(echo "${CF_DOMAIN}" | cut -d. -f1)${NC} -> ${CYAN}TUNNEL_UUID.cfargotunnel.com${NC} (Proxied)"
    echo ""
}

create_xhttp_config() {
    local clients_json="[]"

    if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        clients_json="["
        local first=true
        local client_uuid

        for client_file in ${CLIENT_DIR}/*.conf; do
            client_uuid=$(grep "^CLIENT_UUID=" "$client_file" | cut -d= -f2 | tr -d '"')

            if [[ "$first" == "true" ]]; then
                first=false
            else
                clients_json+=","
            fi
            clients_json+="{\"id\":\"${client_uuid}\"}"
        done

        clients_json+="]"
    fi

    source "${XHTTP_PARAMS}"

    cat > "${XHTTP_CONFIG}" << EOF
{
    "log": {
        "loglevel": "warning"
    },
    "dns": {
        "servers": ["192.168.2.249", "1.1.1.1"]
    },
    "inbounds": [
        {
            "listen": "127.0.0.1",
            "port": ${XHTTP_PORT},
            "protocol": "vless",
            "settings": {
                "clients": ${clients_json},
                "decryption": "none"
            },
            "streamSettings": {
                "network": "grpc",
                "security": "tls",
                "tlsSettings": {
                    "certificates": [
                        {
                            "certificateFile": "${XHTTP_DIR}/cert.pem",
                            "keyFile": "${XHTTP_DIR}/key.pem"
                        }
                    ]
                },
                "grpcSettings": {
                    "serviceName": "${SERVICE_NAME}",
                    "multiMode": true,
                    "idle_timeout": 60,
                    "health_check_timeout": 20,
                    "initial_windows_size": 65536
                }
            },
            "sniffing": {
                "enabled": true,
                "destOverride": ["http", "tls", "quic"]
            }
        }
    ],
    "outbounds": [
        {
            "protocol": "freedom",
            "tag": "direct"
        },
        {
            "protocol": "blackhole",
            "tag": "block"
        }
    ]
}
EOF
    chmod 600 "${XHTTP_CONFIG}"
    chown xray:"${GROUP_NAME}" "${XHTTP_CONFIG}" 2>/dev/null || chown xray:nogroup "${XHTTP_CONFIG}"
}

start_xhttp() {
    log_info "Starting XRay CDN Tunnel..."
    systemctl daemon-reload
    systemctl enable xray-xhttp
    systemctl restart xray-xhttp
    sleep 2
    service_is_active xray-xhttp && log_success "XRay CDN Tunnel running" || { log_error "Failed to start"; journalctl -u xray-xhttp --no-pager -n 10; exit 1; }
}

run_install() {
    check_root
    check_os
    install_essentials
    install_xray_binary
    install_cloudflared
    create_xhttp_user
    create_xhttp_service
    configure_server

    if [[ -f "${XHTTP_PARAMS}" ]]; then
        source "${XHTTP_PARAMS}"
    else
        log_error "Params file not found: ${XHTTP_PARAMS}"
        exit 1
    fi

    start_xhttp

    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  XRay CDN Tunnel Installation Complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
    echo -e "Domain:   ${CYAN}${CF_DOMAIN}${NC}"
    echo -e "Transport: ${CYAN}gRPC (multiMode)${NC}"
    echo -e "Service:  ${CYAN}${SERVICE_NAME}${NC}"
    echo ""
    echo -e "${YELLOW}Verify:${NC}"
    echo -e "  CF dashboard: gRPC ON, SSL Full, Browser Integrity Check OFF"
    echo -e "  CF API: http2Origin + noTLSVerify on tunnel route"
    echo -e "  cloudflared: --protocol http2 (set automatically)"
    echo ""
    echo -e "${YELLOW}Now add a client to connect:${NC}"
    echo ""

    create_client
}


# ============================================================================
# CLIENT MANAGEMENT
# ============================================================================

create_client() {
    load_params

    echo ""
    read -rp "Client name: " input_name
    [[ -z "${input_name}" ]] && { log_error "Name required"; return 1; }

    local new_client_name new_client_file new_client_uuid new_created_date
    new_client_name=$(sanitize_client_name "${input_name}")
    new_client_file="${CLIENT_DIR}/${new_client_name}.conf"

    [[ -f "${new_client_file}" ]] && { log_error "Client '${new_client_name}' already exists"; return 1; }

    new_client_uuid=$(xray uuid)
    new_created_date=$(date +%Y-%m-%d)

    cat > "${new_client_file}" << EOF
CLIENT_NAME="${new_client_name}"
CLIENT_UUID="${new_client_uuid}"
CREATED_DATE="${new_created_date}"
EOF
    chmod 600 "${new_client_file}"

    create_xhttp_config
    systemctl restart xray-xhttp

    log_success "Client '${new_client_name}' created"
    show_client_config "${new_client_name}"
}

show_client_config() {
    local name="$1"
    if [[ -z "$name" ]]; then
        if ! select_client "Show config for client"; then
            return
        fi
        name="${SELECTED_CLIENT_NAME}"
    fi

    local client_file="${CLIENT_DIR}/${name}.conf"
    [[ ! -f "${client_file}" ]] && { log_error "Client '${name}' not found"; return 1; }

    source "${XHTTP_PARAMS}"

    local CLIENT_UUID CLIENT_NAME CREATED_DATE
    source "${client_file}"
    CLIENT_NAME="${name}"

    local PROFILE_NAME="${CF_DOMAIN}-${CLIENT_NAME}"
    local VLESS_URL="vless://${CLIENT_UUID}@${CF_DOMAIN}:443?encryption=none&security=tls&sni=${CF_DOMAIN}&type=grpc&serviceName=${SERVICE_NAME}&mode=multi&fp=${XHTTP_FINGERPRINT}#${PROFILE_NAME}"

    echo ""
    echo -e "${GREEN}=== Client: ${CLIENT_NAME} ===${NC}"
    echo ""
    echo -e "Domain:      ${CYAN}${CF_DOMAIN}${NC}"
    echo -e "Port:        ${CYAN}443 (Cloudflare CDN)${NC}"
    echo -e "UUID:        ${CYAN}${CLIENT_UUID}${NC}"
    echo -e "Transport:   ${CYAN}gRPC (multiMode)${NC}"
    echo -e "Security:    ${CYAN}tls (Cloudflare)${NC}"
    echo -e "Service:     ${CYAN}${SERVICE_NAME}${NC}"
    echo -e "Created:     ${CYAN}${CREATED_DATE}${NC}"
    echo ""
    echo -e "${GREEN}VLESS URL (copy to AmneziaVPN / v2rayNG / Hiddify):${NC}"
    echo -e "${CYAN}${VLESS_URL}${NC}"
    echo ""

    echo "${VLESS_URL}" > "${CLIENT_DIR}/${CLIENT_NAME}-vless.txt"

    if command -v qrencode &>/dev/null; then
        echo -e "${GREEN}QR Code:${NC}"
        echo "${VLESS_URL}" | qrencode -t ansiutf8
    fi

    cat > "${CLIENT_DIR}/${CLIENT_NAME}-config.json" << EOF
{
    "outbounds": [
        {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "${CF_DOMAIN}",
                        "port": 443,
                        "users": [
                            {
                                "id": "${CLIENT_UUID}",
                                "encryption": "none"
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {
                "network": "grpc",
                "security": "tls",
                "tlsSettings": {
                    "serverName": "${CF_DOMAIN}",
                    "fingerprint": "${XHTTP_FINGERPRINT}"
                },
                "grpcSettings": {
                    "serviceName": "${SERVICE_NAME}",
                    "multiMode": true,
                    "initial_windows_size": 65536
                }
            }
        }
    ]
}
EOF
}

list_clients() {
    load_params

    echo ""
    echo -e "${GREEN}=== CDN Tunnel Clients ===${NC}"
    echo ""

    if [[ ! -d ${CLIENT_DIR} ]] || ! ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        echo -e "  ${YELLOW}No clients configured${NC}"
        return
    fi

    printf "  %-4s %-20s %-12s\n" "#" "NAME" "CREATED"
    printf "  %-4s %-20s %-12s\n" "----" "--------------------" "------------"

    local i=1 client_name created_date
    for client_file in ${CLIENT_DIR}/*.conf; do
        client_name=$(grep "^CLIENT_NAME=" "$client_file" | cut -d= -f2 | tr -d '"')
        created_date=$(grep "^CREATED_DATE=" "$client_file" | cut -d= -f2 | tr -d '"')
        printf "  %-4s %-20s %-12s\n" "${i})" "${client_name}" "${created_date}"
        i=$((i + 1))
    done
    echo ""
}

select_client() {
    local prompt="${1:-Select client}"
    load_params

    if [[ ! -d ${CLIENT_DIR} ]] || ! ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        echo -e "  ${YELLOW}No clients configured${NC}"
        return 1
    fi

    list_clients

    local clients=()
    for client_file in ${CLIENT_DIR}/*.conf; do
        clients+=("$(grep "^CLIENT_NAME=" "$client_file" | cut -d= -f2 | tr -d '"')")
    done

    local selection
    read -rp "${prompt} [1-${#clients[@]}]: " selection
    [[ -z "${selection}" ]] && return 1

    if ! [[ "${selection}" =~ ^[0-9]+$ ]] || (( selection < 1 || selection > ${#clients[@]} )); then
        log_error "Invalid selection"
        return 1
    fi

    SELECTED_CLIENT_NAME="${clients[$((selection - 1))]}"
    return 0
}

revoke_client() {
    if ! select_client "Client to revoke"; then
        return
    fi

    local revoke_name="${SELECTED_CLIENT_NAME}"
    local revoke_file="${CLIENT_DIR}/${revoke_name}.conf"

    confirm_action "Revoke client '${revoke_name}'?" || return

    rm -f "${revoke_file}"
    rm -f "${CLIENT_DIR}/${revoke_name}-vless.txt"
    rm -f "${CLIENT_DIR}/${revoke_name}-config.json"

    create_xhttp_config
    systemctl restart xray-xhttp

    log_success "Client '${revoke_name}' revoked"
}


# ============================================================================
# MANAGEMENT FUNCTIONS
# ============================================================================

load_params() {
    [[ ! -f "${XHTTP_PARAMS}" ]] && { log_error "CDN Tunnel not installed"; exit 1; }
    source "${XHTTP_PARAMS}"
}

show_status() {
    echo ""
    echo -e "${GREEN}=== XRay CDN Tunnel Status ===${NC}"
    echo ""

    echo -e "${CYAN}--- xray-xhttp service ---${NC}"
    systemctl status xray-xhttp --no-pager 2>/dev/null | head -10 || echo -e "  ${RED}Not installed${NC}"

    echo ""
    echo -e "${CYAN}--- cloudflared service ---${NC}"
    systemctl status cloudflared --no-pager 2>/dev/null | head -10 || echo -e "  ${RED}Not installed${NC}"

    echo ""
    local client_count=0
    if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        client_count=$(ls ${CLIENT_DIR}/*.conf 2>/dev/null | wc -l)
    fi
    echo -e "Active clients: ${CYAN}${client_count}${NC}"
}

manual_update() {
    log_info "Checking for XRay updates..."

    local current
    current=$(xray version 2>/dev/null | head -1 | awk '{print $2}')
    echo -e "Current version: ${CYAN}${current}${NC}"

    local latest
    latest=$(curl -sL "https://api.github.com/repos/XTLS/Xray-core/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | cut -d'"' -f4)
    latest="${latest#v}"

    if [[ -n "${latest}" ]]; then
        echo -e "Latest version:  ${CYAN}${latest}${NC}"
        if [[ "${current}" == "${latest}" ]]; then
            log_success "Already running the latest version"
            press_enter
            return 0
        fi
        echo -e "${YELLOW}Update available: ${current} -> ${latest}${NC}"
    else
        echo -e "Latest version:  ${YELLOW}(couldn't fetch from GitHub)${NC}"
    fi

    echo ""
    if ! confirm_action "Run update now?"; then
        return 0
    fi

    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install

    local new
    new=$(xray version 2>/dev/null | head -1 | awk '{print $2}')

    if [[ "${current}" != "${new}" ]]; then
        log_success "Updated to ${new}"
        systemctl restart xray-xhttp
        if service_is_active xray 2>/dev/null; then
            log_warning "xray.service (VLESS+REALITY) is also running - consider restarting it too"
        fi
    else
        log_info "Already at latest version"
    fi

    press_enter
}

uninstall_xhttp() {
    log_warning "This will remove CDN Tunnel configuration and Cloudflare Tunnel!"
    log_warning "The XRay binary will NOT be removed (may be used by VLESS+REALITY)"
    confirm_action "Continue?" || return

    systemctl stop xray-xhttp 2>/dev/null || true
    systemctl disable xray-xhttp 2>/dev/null || true
    rm -f /etc/systemd/system/xray-xhttp.service

    systemctl stop cloudflared 2>/dev/null || true
    systemctl disable cloudflared 2>/dev/null || true
    rm -f /etc/systemd/system/cloudflared.service

    rm -rf "${XHTTP_DIR}" /etc/vpn/xhttp

    systemctl daemon-reload
    log_success "CDN Tunnel uninstalled (XRay binary preserved)"
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
        echo -e "${GREEN}║     XRay CDN Tunnel (gRPC)             ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
        echo ""

        if [[ -f ${XHTTP_PARAMS} ]]; then
            source "${XHTTP_PARAMS}"
            service_is_active xray-xhttp && echo -e "  XRay gRPC:  ${GREEN}Running${NC}" || echo -e "  XRay gRPC:  ${RED}Stopped${NC}"
            service_is_active cloudflared 2>/dev/null && echo -e "  CF Tunnel:  ${GREEN}Running${NC}" || echo -e "  CF Tunnel:  ${RED}Stopped${NC}"

            local client_count=0
            if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
                client_count=$(ls ${CLIENT_DIR}/*.conf 2>/dev/null | wc -l)
            fi
            echo -e "  Clients:    ${CYAN}${client_count}${NC}"
            echo -e "  Domain:     ${CYAN}${CF_DOMAIN}${NC}"
            echo -e "  Transport:  ${CYAN}gRPC (multiMode)${NC}"
            echo ""
            echo "  1) Add client"
            echo "  2) List clients"
            echo "  3) Show client config & QR"
            echo "  4) Revoke client"
            echo "  5) Show status"
            echo "  6) Restart services"
            echo "  7) Update XRay"
            echo "  8) Uninstall"
        else
            echo -e "  Status: ${YELLOW}Not installed${NC}"
            echo ""
            echo -e "  ${CYAN}Prerequisites (do these first in Cloudflare dashboard):${NC}"
            echo "    1. Add your domain to Cloudflare (free plan)"
            echo "    2. Enable gRPC:  domain > Network > toggle gRPC ON"
            echo "    3. Set SSL mode: domain > SSL/TLS > Full"
            echo "    4. Disable Browser Integrity Check: domain > Security > Settings"
            echo "    5. Create tunnel: Zero Trust > Networking > Tunnels"
            echo "       Copy the tunnel install token"
            echo "    6. After install: set http2Origin + noTLSVerify via CF API"
            echo ""
            echo "  1) Install XRay CDN Tunnel"
        fi
        echo ""
        echo "  0) Exit"
        echo ""
        read -rp "Select: " choice

        if [[ -f ${XHTTP_PARAMS} ]]; then
            case ${choice} in
                1) create_client ;;
                2) list_clients ;;
                3)
                    list_clients
                    show_client_config
                    ;;
                4) revoke_client ;;
                5) show_status ;;
                6)
                    systemctl restart xray-xhttp 2>/dev/null && log_success "xray-xhttp restarted" || log_error "Failed to restart xray-xhttp"
                    systemctl restart cloudflared 2>/dev/null && log_success "cloudflared restarted" || log_error "Failed to restart cloudflared"
                    ;;
                7) manual_update ;;
                8) uninstall_xhttp ;;
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
