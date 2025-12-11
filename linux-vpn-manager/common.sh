#!/bin/bash
#
# Linux VPN Server Manager - Common Functions Library
# Shared functions used by all VPN scripts (includes optimizations)
#
# Version: 5.0
# Updated: December 2025
#

# Prevent multiple sourcing
[[ -n "${_COMMON_SH_LOADED:-}" ]] && return 0
_COMMON_SH_LOADED=1

# ============================================================================
# COLORS
# ============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ============================================================================
# LOGGING FUNCTIONS
# ============================================================================
log_info() { echo -e "${CYAN}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_debug() { [[ "${DEBUG:-0}" == "1" ]] && echo -e "${CYAN}[DEBUG]${NC} $1"; }

# ============================================================================
# VALIDATION FUNCTIONS
# ============================================================================

check_root() {
    if [[ "$EUID" -ne 0 ]]; then
        log_error "This script must be run as root"
        exit 1
    fi
}

check_os() {
    if [[ -e /etc/os-release ]]; then
        source /etc/os-release
        OS="${ID}"
        OS_VERSION="${VERSION_ID}"
    else
        log_error "Cannot detect OS"
        exit 1
    fi

    case ${OS} in
        ubuntu)
            [[ "${OS_VERSION%%.*}" -lt 20 ]] && { log_error "Ubuntu 20.04+ required"; exit 1; }
            PKG_MANAGER="apt-get"; GROUP_NAME="nogroup"
            ;;
        debian)
            [[ "${OS_VERSION}" -lt 10 ]] && { log_error "Debian 10+ required"; exit 1; }
            PKG_MANAGER="apt-get"; GROUP_NAME="nogroup"
            ;;
        almalinux|rocky|centos)
            [[ "${OS_VERSION%%.*}" -lt 8 ]] && { log_error "${OS} 8+ required"; exit 1; }
            PKG_MANAGER="dnf"; GROUP_NAME="nobody"
            ;;
        *)
            log_error "Unsupported OS: ${OS}"
            exit 1
            ;;
    esac

    log_info "Detected: ${OS} ${OS_VERSION}"
    export OS OS_VERSION PKG_MANAGER GROUP_NAME
}

# ============================================================================
# NETWORK FUNCTIONS
# ============================================================================

get_public_ip() {
    PUBLIC_IP=""
    for source in "https://api.ipify.org" "https://ifconfig.me" "https://icanhazip.com"; do
        PUBLIC_IP=$(curl -4 -s --max-time 5 "${source}" 2>/dev/null | tr -d '[:space:]')
        [[ "${PUBLIC_IP}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && break
        PUBLIC_IP=""
    done
    
    if [[ -z "${PUBLIC_IP}" ]]; then
        log_warning "Could not detect public IP"
        read -rp "Enter server's public IP: " PUBLIC_IP
    fi
    export PUBLIC_IP
}

get_server_nic() {
    SERVER_NIC=$(ip -4 route ls | grep default | grep -Po '(?<=dev )(\S+)' | head -1)
    [[ -z "${SERVER_NIC}" ]] && SERVER_NIC=$(ip link | grep -v lo | grep 'state UP' | head -1 | awk -F': ' '{print $2}')
    [[ -z "${SERVER_NIC}" ]] && { log_error "Could not detect network interface"; exit 1; }
    export SERVER_NIC
}

check_ipv6() {
    IPV6_SUPPORT=false
    PUBLIC_IPV6=""
    
    [[ ! -f /proc/net/if_inet6 ]] && { export IPV6_SUPPORT PUBLIC_IPV6; return; }
    
    if ip -6 addr | grep -q 'inet6'; then
        PUBLIC_IPV6=$(ip -6 addr | grep 'inet6 [23]' | grep -v 'deprecated' | cut -d '/' -f 1 | awk '{print $2}' | head -1)
        [[ -z "${PUBLIC_IPV6}" ]] && PUBLIC_IPV6=$(ip -6 addr | grep 'inet6 fd' | cut -d '/' -f 1 | awk '{print $2}' | head -1)
        IPV6_SUPPORT=true
    fi
    export IPV6_SUPPORT PUBLIC_IPV6
}

# ============================================================================
# PACKAGE MANAGEMENT
# ============================================================================

install_essentials() {
    local packages=(curl wget tar jq iptables qrencode)
    [[ "${PKG_MANAGER}" == "apt-get" ]] && packages+=(xz-utils) || packages+=(xz)
    
    log_info "Installing essentials..."
    case ${PKG_MANAGER} in
        apt-get) apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y "${packages[@]}" ;;
        dnf) dnf install -y epel-release 2>/dev/null; dnf install -y "${packages[@]}" ;;
    esac
}

# ============================================================================
# FIREWALL FUNCTIONS
# ============================================================================

firewall_open_port() {
    local port="$1" protocol="${2:-tcp}"
    
    case ${OS} in
        ubuntu|debian)
            command -v ufw &>/dev/null && ufw status | grep -q "active" && ufw allow "${port}/${protocol}"
            ;;
        almalinux|rocky|centos)
            command -v firewall-cmd &>/dev/null && systemctl is-active --quiet firewalld && {
                firewall-cmd --permanent --add-port="${port}/${protocol}"
                firewall-cmd --reload
            }
            ;;
    esac
}

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

generate_password() { head -c 100 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c "${1:-32}"; }

sanitize_client_name() {
    local name="$1" max_length="${2:-32}"
    name=$(echo "${name}" | tr -dc 'a-zA-Z0-9_-')
    echo "${name:0:${max_length}}"
}

validate_port() {
    local port="$1"
    [[ "${port}" =~ ^[0-9]+$ ]] && [[ ${port} -ge 1 ]] && [[ ${port} -le 65535 ]]
}

service_is_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

press_enter() { echo ""; read -rp "Press Enter to continue..."; }

confirm_action() {
    local msg="${1:-Continue?}" default="${2:-n}"
    read -rp "${msg} [y/N]: " response
    [[ "${response:-$default}" =~ ^[Yy]$ ]]
}

# ============================================================================
# INSTALLATION STATE DETECTION
# ============================================================================

detect_installed() {
    WIREGUARD_INSTALLED=false
    OPENVPN_INSTALLED=false
    SHADOWSOCKS_INSTALLED=false
    XRAY_INSTALLED=false
    
    [[ -f /etc/wireguard/params ]] || service_is_active wg-quick@wg0 && WIREGUARD_INSTALLED=true
    [[ -f /etc/openvpn/server.conf ]] || service_is_active openvpn-server@server && OPENVPN_INSTALLED=true
    [[ -f /etc/shadowsocks/config.json ]] || service_is_active shadowsocks && SHADOWSOCKS_INSTALLED=true
    [[ -f /etc/xray/params ]] || service_is_active xray && XRAY_INSTALLED=true
    
    export WIREGUARD_INSTALLED OPENVPN_INSTALLED SHADOWSOCKS_INSTALLED XRAY_INSTALLED
}

# ============================================================================
# SYSTEM OPTIMIZATIONS (merged from optimizations.sh)
# ============================================================================

apply_kernel_optimizations() {
    log_info "Applying kernel optimizations..."
    
    cat > /etc/sysctl.d/99-vpn-optimizations.conf << 'EOF'
# VPN System Optimizations v5.0
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_rmem = 4096 1048576 16777216
net.ipv4.tcp_wmem = 4096 1048576 16777216
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.core.somaxconn = 4096
net.core.netdev_max_backlog = 16384
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF

    sysctl -p /etc/sysctl.d/99-vpn-optimizations.conf >/dev/null 2>&1
    log_success "Kernel optimizations applied"
}

apply_mss_clamping() {
    local iface="${1:-}"
    [[ -z "${iface}" ]] && return
    ip link show "${iface}" &>/dev/null || return
    
    iptables -t mangle -D FORWARD -o "${iface}" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu 2>/dev/null || true
    iptables -t mangle -A FORWARD -o "${iface}" -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
}

save_iptables_rules() {
    if command -v netfilter-persistent &>/dev/null; then
        netfilter-persistent save 2>/dev/null
    elif [[ -d /etc/iptables ]]; then
        iptables-save > /etc/iptables/rules.v4 2>/dev/null
    fi
}

apply_all_optimizations() {
    echo ""
    echo -e "${GREEN}=== Applying Network Optimizations ===${NC}"
    echo ""
    
    # Clear existing file and add header
    echo "# VPN System Optimizations v5.0" > /etc/sysctl.d/99-vpn-optimizations.conf
    echo "# Generated by Linux VPN Manager" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo "" >> /etc/sysctl.d/99-vpn-optimizations.conf
    
    # BBR & Kernel
    echo -n "  - Enabling BBR congestion control... "
    echo "net.core.default_qdisc = fq" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo "net.ipv4.tcp_congestion_control = bbr" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Enabling TCP Fast Open... "
    echo "net.ipv4.tcp_fastopen = 3" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Enabling TCP TIME-WAIT reuse... "
    echo "net.ipv4.tcp_tw_reuse = 1" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Enabling MTU probing... "
    echo "net.ipv4.tcp_mtu_probing = 1" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Tuning TCP read buffers (4K-16M)... "
    echo "net.ipv4.tcp_rmem = 4096 1048576 16777216" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Tuning TCP write buffers (4K-16M)... "
    echo "net.ipv4.tcp_wmem = 4096 1048576 16777216" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Increasing socket buffer limits... "
    echo "net.core.rmem_max = 16777216" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo "net.core.wmem_max = 16777216" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Increasing connection backlog... "
    echo "net.core.somaxconn = 4096" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo "net.core.netdev_max_backlog = 16384" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Enabling IP forwarding (IPv4+IPv6)... "
    echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo "net.ipv6.conf.all.forwarding = 1" >> /etc/sysctl.d/99-vpn-optimizations.conf
    echo -e "${GREEN}done${NC}"
    
    echo -n "  - Applying sysctl settings... "
    sysctl -p /etc/sysctl.d/99-vpn-optimizations.conf >/dev/null 2>&1
    echo -e "${GREEN}done${NC}"
    
    # MSS Clamping
    if [[ -e /sys/class/net/wg0 ]]; then
        echo -n "  - Adding MSS clamping for wg0... "
        apply_mss_clamping wg0
        echo -e "${GREEN}done${NC}"
    fi
    
    if [[ -e /sys/class/net/tun0 ]]; then
        echo -n "  - Adding MSS clamping for tun0... "
        apply_mss_clamping tun0
        echo -e "${GREEN}done${NC}"
    fi
    
    echo -n "  - Saving iptables rules... "
    save_iptables_rules
    echo -e "${GREEN}done${NC}"
    
    echo ""
    echo -e "${GREEN}✓ All optimizations applied successfully${NC}"
    return 0
}

# ============================================================================
# AMNEZIAWG PARAMETERS
# ============================================================================

AWG_JC=4; AWG_JMIN=6; AWG_JMAX=18; AWG_S1=0; AWG_S2=0
AWG_H1=1; AWG_H2=2; AWG_H3=3; AWG_H4=4; AWG_MTU=1320

[[ -f /etc/wireguard/awg-params ]] && source /etc/wireguard/awg-params

export AWG_JC AWG_JMIN AWG_JMAX AWG_S1 AWG_S2 AWG_H1 AWG_H2 AWG_H3 AWG_H4 AWG_MTU

# ============================================================================
# TRAP HANDLERS
# ============================================================================

_CLEANUP_FUNCTIONS=()
register_cleanup() { _CLEANUP_FUNCTIONS+=("$1"); }
_run_cleanup() { for func in "${_CLEANUP_FUNCTIONS[@]}"; do "${func}" 2>/dev/null || true; done; }
setup_traps() { trap _run_cleanup EXIT; trap 'exit 1' INT TERM; }

# Version
VPN_MANAGER_VERSION="5.0"
export VPN_MANAGER_VERSION
