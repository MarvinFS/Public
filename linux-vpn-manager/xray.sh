#!/bin/bash
#
# XRay (VLESS + REALITY) - Install & Manage
#
# Multi-user support with unique shortIds per client for tracking
# Compatible with AmneziaVPN client (Windows/Android/iOS)
#

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" || { echo "ERROR: common.sh not found"; exit 1; }

XRAY_DIR="/etc/xray"
XRAY_CONFIG="${XRAY_DIR}/config.json"
XRAY_PARAMS="${XRAY_DIR}/params"
CLIENT_DIR="/etc/vpn/xray/clients"

# REALITY settings
REALITY_SNI="browser.yandex.com"
REALITY_SERVER_NAME="browser.yandex.com"
REALITY_FINGERPRINT="chrome"
FLOW="xtls-rprx-vision"


# ============================================================================
# INSTALLATION FUNCTIONS
# ============================================================================

install_xray() {
    log_info "Installing XRay via official script..."
    
    # Run install script - ignore non-zero exit codes from warnings about missing files
    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install || true
    
    # Verify xray binary is actually installed (check both PATH and direct location)
    if ! command -v xray &>/dev/null && [[ ! -x /usr/local/bin/xray ]]; then
        log_error "XRay installation failed - binary not found"
        exit 1
    fi
    
    # Ensure /usr/local/bin is in PATH for this session
    export PATH="/usr/local/bin:$PATH"
    
    log_success "XRay installed"
    
    # Show version
    xray version | head -1
    
    # Create dedicated user for XRay (fixes systemd warning about 'nobody')
    create_xray_user
    
    # Create custom systemd service with proper user and config path
    create_xray_service
}

create_xray_user() {
    if ! id "xray" &>/dev/null; then
        log_info "Creating xray system user..."
        useradd --system --no-create-home --shell /usr/sbin/nologin xray
        log_success "User 'xray' created"
    fi
}

create_xray_service() {
    log_info "Creating custom systemd service..."
    
    # Stop default service if running
    systemctl stop xray 2>/dev/null || true
    systemctl disable xray 2>/dev/null || true
    
    # Remove ALL xray service files from everywhere
    rm -rf /etc/systemd/system/xray.service.d
    rm -rf /etc/systemd/system/xray@.service.d
    rm -f /etc/systemd/system/xray.service
    rm -f /etc/systemd/system/xray@.service
    rm -f /usr/lib/systemd/system/xray.service
    rm -f /usr/lib/systemd/system/xray@.service
    rm -rf /usr/lib/systemd/system/xray.service.d
    rm -rf /usr/lib/systemd/system/xray@.service.d
    
    # Ensure xray user owns config directories
    mkdir -p "${XRAY_DIR}" "${CLIENT_DIR}"
    chown -R xray:"${GROUP_NAME}" "${XRAY_DIR}" 2>/dev/null || true
    chown -R xray:"${GROUP_NAME}" /etc/vpn/xray 2>/dev/null || true
    
    # Create our custom service
    cat > /etc/systemd/system/xray.service << EOF
[Unit]
Description=XRay VLESS+REALITY Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xray
Group=${GROUP_NAME}
LimitNOFILE=32768
ExecStart=/usr/local/bin/xray run -config ${XRAY_CONFIG}
Restart=always
RestartSec=3
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
    
    # Reload systemd to pick up new service file
    systemctl daemon-reload
    log_success "Custom service created"
}

generate_keys() {
    log_info "Generating x25519 keypair for REALITY..."
    
    local keypair
    keypair=$(xray x25519) || { log_error "Failed to generate keys"; exit 1; }
    
    # Output format: "PrivateKey: xxx\nPassword: yyy\nHash32: zzz"
    # Password is the public key in XRay terminology
    PRIVATE_KEY=$(echo "$keypair" | grep "PrivateKey:" | awk '{print $2}')
    PUBLIC_KEY=$(echo "$keypair" | grep "Password:" | awk '{print $2}')
    
    [[ -z "${PRIVATE_KEY}" || -z "${PUBLIC_KEY}" ]] && { log_error "Key extraction failed"; exit 1; }
    
    log_success "Keys generated"
}

configure_server() {
    echo ""
    echo -e "${GREEN}=== XRay VLESS+REALITY Configuration ===${NC}"
    echo ""
    
    mkdir -p ${XRAY_DIR} ${CLIENT_DIR}
    chown xray:${GROUP_NAME} ${XRAY_DIR} 2>/dev/null || chown xray:nogroup ${XRAY_DIR}
    chown root:root ${CLIENT_DIR}
    chmod 700 ${XRAY_DIR}
    chmod 700 ${CLIENT_DIR}
    
    read -rp "Port [443]: " XRAY_PORT
    XRAY_PORT=${XRAY_PORT:-443}
    
    echo ""
    echo -e "${CYAN}Server address for client connections:${NC}"
    echo -e "  Can be: domain (vpn.example.com), DDNS (myvpn.ddns.net), or IP"
    read -rp "Server address [${PUBLIC_IP}]: " SERVER_ADDRESS
    SERVER_ADDRESS=${SERVER_ADDRESS:-${PUBLIC_IP}}
    
    generate_keys
    
    # Save params (using single quotes to prevent variable expansion issues)
    cat > ${XRAY_PARAMS} << EOF
XRAY_PORT=${XRAY_PORT}
PRIVATE_KEY='${PRIVATE_KEY}'
PUBLIC_KEY='${PUBLIC_KEY}'
SERVER_ADDRESS='${SERVER_ADDRESS}'
REALITY_SNI='${REALITY_SNI}'
EOF
    chmod 600 ${XRAY_PARAMS}
    chown xray:${GROUP_NAME} ${XRAY_PARAMS} 2>/dev/null || chown xray:nogroup ${XRAY_PARAMS}
    
    # Create initial config (no clients yet)
    create_xray_config
    
    log_success "Server configured"
}

create_xray_config() {
    # Build clients array from client files
    local clients_json="[]"
    local short_ids_json="[\"\"]"  # Default empty shortId for REALITY

    if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        # Build clients array - extract values without sourcing to avoid scope pollution
        clients_json="["
        short_ids_json="["
        local first=true
        local client_uuid client_short_id

        for client_file in ${CLIENT_DIR}/*.conf; do
            # Extract values using grep/cut instead of source
            client_uuid=$(grep "^CLIENT_UUID=" "$client_file" | cut -d= -f2 | tr -d '"')
            client_short_id=$(grep "^CLIENT_SHORT_ID=" "$client_file" | cut -d= -f2 | tr -d '"')

            if [[ "$first" == "true" ]]; then
                first=false
            else
                clients_json+=","
                short_ids_json+=","
            fi
            clients_json+="{\"id\":\"${client_uuid}\",\"flow\":\"${FLOW}\"}"
            short_ids_json+="\"${client_short_id}\""
        done

        clients_json+="]"
        short_ids_json+="]"
    fi

    source "${XRAY_PARAMS}"
    
    cat > "${XRAY_CONFIG}" << EOF
{
    "log": {
        "loglevel": "warning"
    },
    "inbounds": [
        {
            "listen": "::",
            "port": ${XRAY_PORT},
            "protocol": "vless",
            "settings": {
                "clients": ${clients_json},
                "decryption": "none"
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": false,
                    "dest": "${REALITY_SNI}:443",
                    "xver": 0,
                    "serverNames": ["${REALITY_SERVER_NAME}"],
                    "privateKey": "${PRIVATE_KEY}",
                    "shortIds": ${short_ids_json}
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
    chmod 600 ${XRAY_CONFIG}
    chown xray:${GROUP_NAME} ${XRAY_CONFIG} 2>/dev/null || chown xray:nogroup ${XRAY_CONFIG}
}

start_xray() {
    log_info "Starting XRay..."
    systemctl daemon-reload
    systemctl enable xray
    systemctl restart xray
    sleep 2
    service_is_active xray && log_success "XRay running" || { log_error "Failed to start"; journalctl -u xray --no-pager -n 10; exit 1; }
}

run_install() {
    check_root
    check_os
    get_public_ip
    install_essentials
    install_xray
    configure_server
    
    # Reload params to ensure variables are set
    if [[ -f "${XRAY_PARAMS}" ]]; then
        source "${XRAY_PARAMS}"
    else
        log_error "Params file not found: ${XRAY_PARAMS}"
        exit 1
    fi
    
    firewall_open_port "${XRAY_PORT}" tcp || true
    start_xray
    
    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  XRay VLESS+REALITY Installation Complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
    echo -e "Server: ${CYAN}${SERVER_ADDRESS}:${XRAY_PORT}${NC}"
    echo -e "SNI: ${CYAN}${REALITY_SNI}${NC}"
    echo ""
    echo -e "${YELLOW}Now add a client to connect:${NC}"
    echo ""
    
    # Prompt to create first client
    create_client
}

# ============================================================================
# CLIENT MANAGEMENT
# ============================================================================

generate_short_id() {
    # Generate 8-character hex shortId (pure shell, no external deps)
    od -An -tx1 -N4 /dev/urandom | tr -d ' \n'
}

create_client() {
    load_params
    
    echo ""
    read -rp "Client name: " input_name
    [[ -z "${input_name}" ]] && { log_error "Name required"; return 1; }
    
    # Use unique variable names to avoid being overwritten by source commands
    local new_client_name new_client_file new_client_uuid new_client_short_id new_created_date
    new_client_name=$(sanitize_client_name "${input_name}")
    new_client_file="${CLIENT_DIR}/${new_client_name}.conf"
    
    [[ -f "${new_client_file}" ]] && { log_error "Client '${new_client_name}' already exists"; return 1; }
    
    new_client_uuid=$(xray uuid)
    new_client_short_id=$(generate_short_id)
    new_created_date=$(date +%Y-%m-%d)
    
    # Save client config
    cat > "${new_client_file}" << EOF
CLIENT_NAME="${new_client_name}"
CLIENT_UUID="${new_client_uuid}"
CLIENT_SHORT_ID="${new_client_short_id}"
CREATED_DATE="${new_created_date}"
EOF
    chmod 600 "${new_client_file}"
    
    # Rebuild server config (this sources all client files, will set CLIENT_NAME etc)
    create_xray_config
    systemctl restart xray
    
    log_success "Client '${new_client_name}' created"
    
    # Show client config
    show_client_config "${new_client_name}"
}

show_client_config() {
    local name="$1"
    [[ -z "$name" ]] && { read -rp "Client name: " name; }
    
    local client_file="${CLIENT_DIR}/${name}.conf"
    [[ ! -f "${client_file}" ]] && { log_error "Client '${name}' not found"; return 1; }
    
    source "${XRAY_PARAMS}"
    
    # Source client file but preserve the name we were given
    local CLIENT_UUID CLIENT_SHORT_ID CREATED_DATE
    source "${client_file}"
    # Use the passed name, not the one from the file (in case of variable collision)
    local CLIENT_NAME="${name}"
    
    # Build VLESS URL (profile name: server-username)
    local PROFILE_NAME="${SERVER_ADDRESS}-${CLIENT_NAME}"
    local VLESS_URL="vless://${CLIENT_UUID}@${SERVER_ADDRESS}:${XRAY_PORT}?encryption=none&security=reality&sni=${REALITY_SNI}&fp=${REALITY_FINGERPRINT}&pbk=${PUBLIC_KEY}&sid=${CLIENT_SHORT_ID}&flow=${FLOW}&type=tcp#${PROFILE_NAME}"
    
    echo ""
    echo -e "${GREEN}=== Client: ${CLIENT_NAME} ===${NC}"
    echo ""
    echo -e "Server:      ${CYAN}${SERVER_ADDRESS}${NC}"
    echo -e "Port:        ${CYAN}${XRAY_PORT}${NC}"
    echo -e "UUID:        ${CYAN}${CLIENT_UUID}${NC}"
    echo -e "Flow:        ${CYAN}${FLOW}${NC}"
    echo -e "Security:    ${CYAN}reality${NC}"
    echo -e "SNI:         ${CYAN}${REALITY_SNI}${NC}"
    echo -e "Fingerprint: ${CYAN}${REALITY_FINGERPRINT}${NC}"
    echo -e "Public Key:  ${CYAN}${PUBLIC_KEY}${NC}"
    echo -e "Short ID:    ${CYAN}${CLIENT_SHORT_ID}${NC}"
    echo -e "Created:     ${CYAN}${CREATED_DATE}${NC}"
    echo ""
    echo -e "${GREEN}VLESS URL (copy to AmneziaVPN):${NC}"
    echo -e "${CYAN}${VLESS_URL}${NC}"
    echo ""
    
    # Save URL to file
    echo "${VLESS_URL}" > "${CLIENT_DIR}/${CLIENT_NAME}-vless.txt"
    
    # Generate QR code
    if command -v qrencode &>/dev/null; then
        echo -e "${GREEN}QR Code (scan with AmneziaVPN mobile):${NC}"
        echo "${VLESS_URL}" | qrencode -t ansiutf8
    fi
    
    # Save JSON config for reference
    cat > "${CLIENT_DIR}/${CLIENT_NAME}-config.json" << EOF
{
    "outbounds": [
        {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "${SERVER_ADDRESS}",
                        "port": ${XRAY_PORT},
                        "users": [
                            {
                                "id": "${CLIENT_UUID}",
                                "encryption": "none",
                                "flow": "${FLOW}"
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "serverName": "${REALITY_SNI}",
                    "fingerprint": "${REALITY_FINGERPRINT}",
                    "publicKey": "${PUBLIC_KEY}",
                    "shortId": "${CLIENT_SHORT_ID}",
                    "spiderX": ""
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
    echo -e "${GREEN}=== XRay Clients ===${NC}"
    echo ""

    if [[ ! -d ${CLIENT_DIR} ]] || ! ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        echo -e "  ${YELLOW}No clients configured${NC}"
        return
    fi

    printf "  %-20s %-10s %-12s\n" "NAME" "SHORT_ID" "CREATED"
    printf "  %-20s %-10s %-12s\n" "--------------------" "----------" "------------"

    local client_name client_short_id created_date
    for client_file in ${CLIENT_DIR}/*.conf; do
        # Extract values without sourcing to avoid scope pollution
        client_name=$(grep "^CLIENT_NAME=" "$client_file" | cut -d= -f2 | tr -d '"')
        client_short_id=$(grep "^CLIENT_SHORT_ID=" "$client_file" | cut -d= -f2 | tr -d '"')
        created_date=$(grep "^CREATED_DATE=" "$client_file" | cut -d= -f2 | tr -d '"')
        printf "  %-20s %-10s %-12s\n" "${client_name}" "${client_short_id}" "${created_date}"
    done
    echo ""
}

revoke_client() {
    load_params
    
    list_clients
    
    read -rp "Client name to revoke: " input_name
    [[ -z "${input_name}" ]] && return
    
    # Use unique variable names to avoid being overwritten by source commands
    local revoke_name="${input_name}"
    local revoke_file="${CLIENT_DIR}/${revoke_name}.conf"
    [[ ! -f "${revoke_file}" ]] && { log_error "Client '${revoke_name}' not found"; return 1; }
    
    confirm_action "Revoke client '${revoke_name}'?" || return
    
    rm -f "${revoke_file}"
    rm -f "${CLIENT_DIR}/${revoke_name}-vless.txt"
    rm -f "${CLIENT_DIR}/${revoke_name}-config.json"
    
    # Rebuild server config
    create_xray_config
    systemctl restart xray
    
    log_success "Client '${revoke_name}' revoked"
}

# ============================================================================
# MANAGEMENT FUNCTIONS
# ============================================================================

load_params() {
    [[ ! -f "${XRAY_PARAMS}" ]] && { log_error "XRay not installed"; exit 1; }
    source "${XRAY_PARAMS}"
}

change_port() {
    load_params
    
    local NEW_PORT
    read -rp "New port [${XRAY_PORT}]: " NEW_PORT
    NEW_PORT=${NEW_PORT:-$XRAY_PORT}
    
    if ! validate_port "${NEW_PORT}"; then
        log_error "Invalid port. Must be 1-65535"
        return 1
    fi
    
    sed -i "s|^XRAY_PORT=.*|XRAY_PORT=${NEW_PORT}|" "${XRAY_PARAMS}"
    XRAY_PORT=${NEW_PORT}
    
    create_xray_config
    firewall_open_port "${NEW_PORT}" tcp
    systemctl restart xray
    
    log_success "Port changed to ${NEW_PORT}"
}

change_server_address() {
    load_params
    
    echo ""
    echo -e "Current server address: ${CYAN}${SERVER_ADDRESS}${NC}"
    echo -e "  Can be: domain (vpn.example.com), DDNS (myvpn.ddns.net), or IP"
    read -rp "New server address: " NEW_ADDRESS
    [[ -z "${NEW_ADDRESS}" ]] && { log_error "Address required"; return 1; }
    
    sed -i "s|^SERVER_ADDRESS=.*|SERVER_ADDRESS=\"${NEW_ADDRESS}\"|" "${XRAY_PARAMS}"
    SERVER_ADDRESS="${NEW_ADDRESS}"
    
    log_success "Server address changed to ${NEW_ADDRESS}"
    log_warning "Regenerate client configs to use new address"
}

regenerate_keys() {
    load_params
    
    log_warning "This will invalidate ALL existing client configs!"
    confirm_action "Continue?" || return
    
    generate_keys
    
    sed -i "s|^PRIVATE_KEY=.*|PRIVATE_KEY='${PRIVATE_KEY}'|" "${XRAY_PARAMS}"
    sed -i "s|^PUBLIC_KEY=.*|PUBLIC_KEY='${PUBLIC_KEY}'|" "${XRAY_PARAMS}"
    
    create_xray_config
    systemctl restart xray
    
    log_success "Keys regenerated - all clients need new configs"
}

show_status() {
    echo ""
    echo -e "${GREEN}=== XRay Status ===${NC}"
    echo ""
    systemctl status xray --no-pager | head -15
    
    echo ""
    local client_count=0
    if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        client_count=$(ls ${CLIENT_DIR}/*.conf 2>/dev/null | wc -l)
    fi
    echo -e "Active clients: ${CYAN}${client_count}${NC}"
}

uninstall_xray() {
    log_warning "This will remove XRay and all configurations!"
    confirm_action "Continue?" || return

    systemctl stop xray 2>/dev/null || true
    systemctl disable xray 2>/dev/null || true

    # Remove auto-update timer if enabled
    systemctl stop xray-update.timer 2>/dev/null || true
    systemctl disable xray-update.timer 2>/dev/null || true
    rm -f /etc/systemd/system/xray-update.timer
    rm -f /etc/systemd/system/xray-update.service
    rm -f /usr/local/bin/xray-update.sh

    # Use official uninstall
    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ remove --purge 2>/dev/null || true

    # Remove service file
    rm -f /etc/systemd/system/xray.service

    # Remove configs
    rm -rf ${XRAY_DIR} /etc/vpn/xray

    # Remove firewall rules
    if command -v ufw &>/dev/null && ufw status | grep -q "active"; then
        ufw delete allow 443/tcp 2>/dev/null || true
    fi
    if command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld; then
        firewall-cmd --permanent --remove-port=443/tcp 2>/dev/null || true
        firewall-cmd --reload 2>/dev/null || true
    fi

    systemctl daemon-reload
    log_success "XRay uninstalled"
}

# ============================================================================
# AUTO-UPDATE
# ============================================================================

setup_auto_update() {
    echo ""
    echo -e "${GREEN}=== XRay Auto-Update Setup ===${NC}"
    echo ""
    echo "  1) Daily updates"
    echo "  2) Weekly updates (Mondays)"
    echo "  3) Disable auto-update"
    echo "  0) Cancel"
    echo ""
    read -rp "Select: " update_choice
    
    case ${update_choice} in
        1)
            create_update_timer "daily"
            ;;
        2)
            create_update_timer "weekly"
            ;;
        3)
            disable_auto_update
            ;;
        *)
            return
            ;;
    esac
}

create_update_timer() {
    local frequency=$1
    
    log_info "Setting up ${frequency} auto-update..."
    
    # Create update script
    cat > /usr/local/bin/xray-update.sh << 'EOFSCRIPT'
#!/bin/bash
# XRay auto-update script

LOG_FILE="/var/log/xray-update.log"

echo "$(date): Starting XRay update check" >> ${LOG_FILE}

# Get current version
CURRENT_VERSION=$(xray version 2>/dev/null | head -1 | awk '{print $2}')

# Run update
bash -c "$(curl -sL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >> ${LOG_FILE} 2>&1

# Get new version
NEW_VERSION=$(xray version 2>/dev/null | head -1 | awk '{print $2}')

if [[ "${CURRENT_VERSION}" != "${NEW_VERSION}" ]]; then
    echo "$(date): Updated from ${CURRENT_VERSION} to ${NEW_VERSION}" >> ${LOG_FILE}
    systemctl restart xray
else
    echo "$(date): Already at latest version ${CURRENT_VERSION}" >> ${LOG_FILE}
fi
EOFSCRIPT
    chmod +x /usr/local/bin/xray-update.sh
    
    # Create systemd service
    cat > /etc/systemd/system/xray-update.service << EOF
[Unit]
Description=XRay Auto-Update
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/xray-update.sh
EOF
    
    # Create timer based on frequency
    if [[ "${frequency}" == "daily" ]]; then
        cat > /etc/systemd/system/xray-update.timer << EOF
[Unit]
Description=XRay Daily Update Timer

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF
    else
        cat > /etc/systemd/system/xray-update.timer << EOF
[Unit]
Description=XRay Weekly Update Timer

[Timer]
OnCalendar=Mon *-*-* 04:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF
    fi
    
    systemctl daemon-reload
    systemctl enable xray-update.timer
    systemctl start xray-update.timer
    
    log_success "Auto-update enabled (${frequency} at 4 AM)"
    echo -e "  Log file: ${CYAN}/var/log/xray-update.log${NC}"
}

disable_auto_update() {
    systemctl stop xray-update.timer 2>/dev/null || true
    systemctl disable xray-update.timer 2>/dev/null || true
    rm -f /etc/systemd/system/xray-update.timer
    rm -f /etc/systemd/system/xray-update.service
    rm -f /usr/local/bin/xray-update.sh
    systemctl daemon-reload
    
    log_success "Auto-update disabled"
}

show_update_status() {
    echo ""
    echo -e "${GREEN}=== XRay Update Status ===${NC}"
    echo ""
    
    local current
    current=$(xray version 2>/dev/null | head -1 | awk '{print $2}')
    echo -e "Current version: ${CYAN}${current}${NC}"
    
    # Fetch latest version from GitHub API
    local latest
    latest=$(curl -sL "https://api.github.com/repos/XTLS/Xray-core/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | cut -d'"' -f4)
    # Remove 'v' prefix (GitHub uses v25.12.8, xray reports 25.12.8)
    latest="${latest#v}"
    
    if [[ -n "${latest}" ]]; then
        if [[ "${current}" == "${latest}" ]]; then
            echo -e "Latest version:  ${GREEN}${latest} ✓${NC}"
        else
            echo -e "Latest version:  ${YELLOW}${latest} (update available)${NC}"
        fi
    else
        echo -e "Latest version:  ${YELLOW}(couldn't fetch)${NC}"
    fi
    
    if systemctl is-active xray-update.timer &>/dev/null; then
        echo -e "Auto-update: ${GREEN}Enabled${NC}"
        systemctl list-timers xray-update.timer --no-pager 2>/dev/null | grep -v "^$"
    else
        echo -e "Auto-update: ${YELLOW}Disabled${NC}"
    fi
    
    if [[ -f /var/log/xray-update.log ]]; then
        echo ""
        echo "Last update log entries:"
        tail -5 /var/log/xray-update.log 2>/dev/null || echo "  (no logs yet)"
    fi
}

manual_update() {
    log_info "Checking for XRay updates..."
    
    local current
    current=$(xray version 2>/dev/null | head -1 | awk '{print $2}')
    echo -e "Current version: ${CYAN}${current}${NC}"
    
    # Fetch latest version from GitHub API
    local latest
    latest=$(curl -sL "https://api.github.com/repos/XTLS/Xray-core/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | cut -d'"' -f4)
    # Remove 'v' prefix (GitHub uses v25.12.8, xray reports 25.12.8)
    latest="${latest#v}"
    
    if [[ -n "${latest}" ]]; then
        echo -e "Latest version:  ${CYAN}${latest}${NC}"
        
        if [[ "${current}" == "${latest}" ]]; then
            echo ""
            log_success "Already running the latest version"
            press_enter
            return 0
        fi
        
        echo ""
        echo -e "${YELLOW}Update available: ${current} → ${latest}${NC}"
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
        systemctl restart xray
    else
        log_info "Already at latest version"
    fi
    
    press_enter
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
        echo -e "${GREEN}║     XRay VLESS + REALITY               ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
        echo ""
        
        if [[ -f ${XRAY_PARAMS} ]]; then
            service_is_active xray && echo -e "  Status: ${GREEN}Running${NC}" || echo -e "  Status: ${RED}Stopped${NC}"
            
            local client_count=0
            if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
                client_count=$(ls ${CLIENT_DIR}/*.conf 2>/dev/null | wc -l)
            fi
            echo -e "  Clients: ${CYAN}${client_count}${NC}"
            echo ""
            echo "  1) Add client"
            echo "  2) List clients"
            echo "  3) Show client config & QR"
            echo "  4) Revoke client"
            echo "  5) Change port"
            echo "  6) Change server address (FQDN/IP)"
            echo "  7) Regenerate keys (invalidates all clients)"
            echo "  8) Show status"
            echo "  9) Restart service"
            echo "  10) Update XRay"
            echo "  11) Auto-update settings"
            echo "  12) Uninstall"
        else
            echo -e "  Status: ${YELLOW}Not installed${NC}"
            echo ""
            echo "  1) Install XRay VLESS+REALITY"
        fi
        echo ""
        echo "  0) Exit"
        echo ""
        read -rp "Select: " choice
        
        if [[ -f ${XRAY_PARAMS} ]]; then
            case ${choice} in
                1) create_client ;;
                2) list_clients ;;
                3) 
                    list_clients
                    show_client_config
                    ;;
                4) revoke_client ;;
                5) change_port ;;
                6) change_server_address ;;
                7) regenerate_keys ;;
                8) show_status ;;
                9) systemctl restart xray; log_success "Restarted" ;;
                10) manual_update ;;
                11) setup_auto_update; show_update_status ;;
                12) uninstall_xray ;;
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
