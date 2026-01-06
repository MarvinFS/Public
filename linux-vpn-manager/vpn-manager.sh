#!/bin/bash
#
# Linux VPN Server Manager - Unified Entry Point
#
# Supports: WireGuard, OpenVPN, Shadowsocks, XRay (VLESS+REALITY)
#

set -e
# Resolve symlinks to get the actual script directory
SCRIPT_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SCRIPT_PATH" ]]; do
    SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
    SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
    [[ $SCRIPT_PATH != /* ]] && SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_PATH"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
source "${SCRIPT_DIR}/common.sh" || { echo "ERROR: common.sh not found"; exit 1; }

# ============================================================================
# STATUS DISPLAY
# ============================================================================

show_status() {
    detect_installed
    
    echo ""
    echo -e "${CYAN}Service Status:${NC}"
    echo ""
    
    if ${WIREGUARD_INSTALLED}; then
        service_is_active wg-quick@wg0 && echo -e "  WireGuard:   ${GREEN}● Running${NC}" || echo -e "  WireGuard:   ${RED}○ Stopped${NC}"
    else
        echo -e "  WireGuard:   ${YELLOW}Not installed${NC}"
    fi
    
    if ${OPENVPN_INSTALLED}; then
        service_is_active openvpn-server@server && echo -e "  OpenVPN:     ${GREEN}● Running${NC}" || echo -e "  OpenVPN:     ${RED}○ Stopped${NC}"
    else
        echo -e "  OpenVPN:     ${YELLOW}Not installed${NC}"
    fi
    
    if ${SHADOWSOCKS_INSTALLED}; then
        service_is_active shadowsocks && echo -e "  Shadowsocks: ${GREEN}● Running${NC}" || echo -e "  Shadowsocks: ${RED}○ Stopped${NC}"
    else
        echo -e "  Shadowsocks: ${YELLOW}Not installed${NC}"
    fi
    
    if ${XRAY_INSTALLED}; then
        service_is_active xray && echo -e "  XRay:        ${GREEN}● Running${NC}" || echo -e "  XRay:        ${RED}○ Stopped${NC}"
    else
        echo -e "  XRay:        ${YELLOW}Not installed${NC}"
    fi
    echo ""
}

# ============================================================================
# SERVICE CONTROLS
# ============================================================================

restart_all() {
    log_info "Restarting all services..."
    try_command "WireGuard" systemctl restart wg-quick@wg0
    try_command "OpenVPN" systemctl restart openvpn-server@server
    try_command "Shadowsocks" systemctl restart shadowsocks
    try_command "XRay" systemctl restart xray
}

stop_all() {
    log_info "Stopping all services..."
    systemctl stop xray 2>/dev/null || true
    systemctl stop wg-quick@wg0 2>/dev/null || true
    systemctl stop openvpn-server@server 2>/dev/null || true
    systemctl stop shadowsocks 2>/dev/null || true
    log_success "All services stopped"
}

start_all() {
    log_info "Starting all services..."
    try_command "WireGuard" systemctl start wg-quick@wg0
    try_command "OpenVPN" systemctl start openvpn-server@server
    try_command "Shadowsocks" systemctl start shadowsocks
    try_command "XRay" systemctl start xray
}

# ============================================================================
# MAIN MENU
# ============================================================================

main_menu() {
    check_root
    
    while true; do
        clear
        echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║          Linux VPN Server Manager                          ║${NC}"
        echo -e "${GREEN}║    WireGuard • OpenVPN • Shadowsocks • XRay                ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
        
        show_status
        
        echo -e "${BOLD}Install / Manage:${NC}"
        echo ""
        echo "  1) WireGuard      - Fast, modern VPN"
        echo "  2) OpenVPN        - Battle-tested VPN"
        echo "  3) Shadowsocks    - Lightweight proxy"
        echo "  4) XRay           - VLESS+REALITY (best obfuscation)"
        echo ""
        echo -e "${BOLD}Service Controls:${NC}"
        echo ""
        echo "  5) Start all services"
        echo "  6) Stop all services"
        echo "  7) Restart all services"
        echo ""
        echo -e "${BOLD}System:${NC}"
        echo ""
        echo "  8) Apply optimizations (BBR, buffers)"
        echo "  9) View logs"
        echo ""
        echo "  0) Exit"
        echo ""
        read -rp "Select: " choice
        
        case ${choice} in
            1) bash "${SCRIPT_DIR}/wireguard.sh" ;;
            2) bash "${SCRIPT_DIR}/openvpn.sh" ;;
            3) bash "${SCRIPT_DIR}/shadowsocks.sh" ;;
            4) bash "${SCRIPT_DIR}/xray.sh" ;;
            5) start_all ;;
            6) stop_all ;;
            7) restart_all ;;
            8) apply_all_optimizations ;;
            9)
                echo ""
                echo "Logs: 1) WireGuard  2) OpenVPN  3) Shadowsocks  4) XRay"
                read -rp "Select: " log_choice
                case ${log_choice} in
                    1) journalctl -u wg-quick@wg0 --no-pager -n 50 ;;
                    2) journalctl -u openvpn-server@server --no-pager -n 50 ;;
                    3) journalctl -u shadowsocks --no-pager -n 50 ;;
                    4) journalctl -u xray --no-pager -n 50 ;;
                esac
                ;;
            0) exit 0 ;;
        esac
        
        press_enter
    done
}

main_menu
