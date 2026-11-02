#!/bin/bash
#
# Linux VPN Server Manager - Common Functions Library
# Shared functions used by all VPN scripts (includes optimizations)
#
# Updated: December 2025
#

# Prevent multiple sourcing
[[ -n "${_COMMON_SH_LOADED:-}" ]] && return 0
_COMMON_SH_LOADED=1

# ============================================================================
# COLORS
# ============================================================================
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[0;33m'
readonly CYAN='\033[0;36m'
readonly BOLD='\033[1m'
readonly NC='\033[0m'

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
    local services=("https://api.ipify.org" "https://ifconfig.me" "https://icanhazip.com")

    # Try IPv4 first
    for source in "${services[@]}"; do
        PUBLIC_IP=$(curl -4 -s --connect-timeout 5 --max-time 10 "${source}" 2>/dev/null | tr -d '[:space:]')
        [[ "${PUBLIC_IP}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && break
        PUBLIC_IP=""
    done

    # Fallback to IPv6 if no IPv4
    if [[ -z "${PUBLIC_IP}" ]]; then
        for source in "${services[@]}"; do
            PUBLIC_IP=$(curl -6 -s --connect-timeout 5 --max-time 10 "${source}" 2>/dev/null | tr -d '[:space:]')
            # Basic IPv6 validation (contains colons)
            [[ "${PUBLIC_IP}" =~ : ]] && break
            PUBLIC_IP=""
        done
    fi

    if [[ -z "${PUBLIC_IP}" ]]; then
        log_warning "Could not detect public IP automatically"
        read -rp "Enter server's public IP: " PUBLIC_IP
    fi
    export PUBLIC_IP
}

get_server_nic() {
    # Allow environment override
    if [[ -n "${SERVER_NIC:-}" ]]; then
        export SERVER_NIC
        return 0
    fi

    # Method 1: Route to external IP
    SERVER_NIC=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'dev \K\S+' | head -1)

    # Method 2: Default route
    if [[ -z "${SERVER_NIC}" ]]; then
        SERVER_NIC=$(ip -4 route show default 2>/dev/null | grep -oP 'dev \K\S+' | head -1)
    fi

    # Method 3: First UP interface (excluding lo)
    if [[ -z "${SERVER_NIC}" ]]; then
        SERVER_NIC=$(ip link show up 2>/dev/null | grep -v lo | grep -oP '^\d+: \K[^:@]+' | head -1)
    fi

    if [[ -z "${SERVER_NIC}" ]]; then
        log_error "Could not detect network interface"
        log_info "Set SERVER_NIC environment variable manually"
        exit 1
    fi
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

verify_checksum() {
    local file="$1" expected="$2"
    local actual
    actual=$(sha256sum "$file" 2>/dev/null | awk '{print $1}')
    if [[ "$actual" != "$expected" ]]; then
        log_error "Checksum mismatch for $file"
        log_error "Expected: $expected"
        log_error "Got: $actual"
        return 1
    fi
    log_success "Checksum verified"
}

try_command() {
    local desc="$1"
    shift
    if "$@" 2>&1; then
        log_success "$desc"
        return 0
    else
        log_warning "$desc failed (continuing)"
        return 0
    fi
}

sanitize_client_name() {
    local name="$1" max_length="${2:-32}"
    # Strict allowlist: only letters, numbers, underscore, hyphen
    if [[ ! "$name" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        log_error "Invalid client name. Use only letters, numbers, underscore, hyphen."
        return 1
    fi
    if [[ ${#name} -gt ${max_length} ]]; then
        log_error "Client name too long (max ${max_length} characters)"
        return 1
    fi
    echo "$name"
}

validate_port() {
    local port="$1"
    [[ "${port}" =~ ^[0-9]+$ ]] && [[ ${port} -ge 1 ]] && [[ ${port} -le 65535 ]]
}

service_is_active() { systemctl is-active --quiet "$1" 2>/dev/null; }

# Returns 0 if port is free, 1 otherwise. On conflict, logs the owning process.
check_port_free() {
    local port="$1" proto="${2:-tcp}"
    local owner
    owner=$(ss -H -ltnp 2>/dev/null | awk -v p=":${port}\$" '$4 ~ p {print $NF; exit}')
    if [[ -n "${owner}" ]]; then
        log_error "Port ${port}/${proto} is already in use by: ${owner}"
        log_error "Stop the conflicting service or uninstall the previous protocol first:"
        log_error "  vpn-manager  ->  Manage installed  ->  Uninstall <protocol>"
        return 1
    fi
    return 0
}

# Polls listening sockets after an uninstall and warns when matching processes
# are still bound. Used to surface "address in use" failures on reinstall.
#   $1: extended-regex of process names to match (passed to grep -E)
#   $2: recovery command to suggest if sockets are still bound
#   $3: optional extra note shown before the recovery hint
warn_if_stuck_sockets() {
    local pattern="$1" recovery="$2" extra_note="${3:-}"
    sleep 1
    local stuck
    stuck=$(ss -H -ltnp 2>/dev/null | grep -E "${pattern}" || true)
    [[ -z "${stuck}" ]] && return 0
    log_warning "These sockets are still bound after uninstall:"
    echo "${stuck}" >&2
    [[ -n "${extra_note}" ]] && log_warning "${extra_note}"
    log_warning "If reinstall fails with 'address in use', run: ${recovery}"
}

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
    XHTTP_INSTALLED=false
    XHTTP_NGINX_INSTALLED=false

    if [[ -f /etc/wireguard/params ]] || service_is_active wg-quick@wg0; then
        WIREGUARD_INSTALLED=true
    fi
    if [[ -f /etc/openvpn/server.conf ]] || service_is_active openvpn-server@server; then
        OPENVPN_INSTALLED=true
    fi
    if [[ -f /etc/shadowsocks/config.json ]] || service_is_active shadowsocks; then
        SHADOWSOCKS_INSTALLED=true
    fi
    if [[ -f /usr/local/etc/xray/params ]] || service_is_active xray; then
        XRAY_INSTALLED=true
    fi
    if [[ -f /usr/local/etc/xray-xhttp/params ]] || service_is_active xray-xhttp; then
        XHTTP_INSTALLED=true
    fi
    if [[ -f /usr/local/etc/xray-xhttp-nginx/params ]] || service_is_active xray-xhttp-nginx; then
        XHTTP_NGINX_INSTALLED=true
    fi

    export WIREGUARD_INSTALLED OPENVPN_INSTALLED SHADOWSOCKS_INSTALLED XRAY_INSTALLED XHTTP_INSTALLED XHTTP_NGINX_INSTALLED
}

# ============================================================================
# SYSTEM OPTIMIZATIONS (merged from optimizations.sh)
# ============================================================================

apply_kernel_optimizations() {
    # Quick/silent version - calls verbose version with flag
    apply_system_optimizations false
}

apply_system_optimizations() {
    local verbose="${1:-true}"

    [[ "$verbose" == "true" ]] && {
        echo ""
        echo -e "${GREEN}=== Applying Network Optimizations ===${NC}"
        echo ""
    }

    # Write sysctl config
    cat > /etc/sysctl.d/99-vpn-optimizations.conf << 'EOF'
# VPN System Optimizations
# Generated by Linux VPN Manager
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

    # MSS Clamping for existing VPN interfaces
    [[ -e /sys/class/net/wg0 ]] && apply_mss_clamping wg0
    [[ -e /sys/class/net/tun0 ]] && apply_mss_clamping tun0

    save_iptables_rules

    if [[ "$verbose" == "true" ]]; then
        log_success "All optimizations applied"
    else
        log_success "Kernel optimizations applied"
    fi
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
    # Alias for verbose optimization (backwards compatibility)
    apply_system_optimizations true
}

# ============================================================================
# AMNEZIAWG 1.5 PARAMETERS
# ============================================================================

# Junk train: Jc packets of Jmin-Jmax bytes before the handshake (client-side only).
# 3 / 40 / 70 is a set reported to work on Yota and Tele2 in Moscow in 2026; the
# vendor says 3-7 is enough. Override in /etc/wireguard/awg-params.
AWG_JC=3; AWG_JMIN=40; AWG_JMAX=70
# I1-I5 are client-side decoy packets sent unencrypted before the WireGuard
# handshake. They stay empty here: every new client gets its own I1 from
# awg-decoy.py (see awg_client_i1), because one static value shared by all
# clients is a fingerprint. To force fixed values, set AWG_I1 (and I2-I5) in
# /etc/wireguard/awg-params; to send no decoy at all, set AWG_DECOY=none there.
AWG_I1=""; AWG_I2=""; AWG_I3=""; AWG_I4=""; AWG_I5=""
AWG_DECOY="dns"
AWG_MTU=1320

[[ -f /etc/wireguard/awg-params ]] && source /etc/wireguard/awg-params

# Print the I1 value for one new client: a fixed AWG_I1 if set, else a fresh
# decoy from awg-decoy.py, else nothing (the client config then has no I1).
# AWG_DECOY_DOMAIN (optional, awg-params) pins the domain the decoy asks for.
awg_client_i1() {
    if [[ -n "${AWG_I1}" ]]; then
        printf '%s' "${AWG_I1}"
        return 0
    fi
    [[ "${AWG_DECOY}" == "none" ]] && return 0
    local helper args=()
    helper="$(dirname "${BASH_SOURCE[0]}")/awg-decoy.py"
    [[ -n "${AWG_DECOY_DOMAIN:-}" ]] && args=(--domain "${AWG_DECOY_DOMAIN}")
    if [[ -r "${helper}" ]] && command -v python3 &>/dev/null; then
        python3 "${helper}" ${args[@]+"${args[@]}"} 2>/dev/null || true
    fi
    return 0
}

export AWG_JC AWG_JMIN AWG_JMAX AWG_I1 AWG_I2 AWG_I3 AWG_I4 AWG_I5 AWG_MTU

# ============================================================================
# TRAP HANDLERS & ROLLBACK
# ============================================================================

_CLEANUP_FUNCTIONS=()
register_cleanup() { _CLEANUP_FUNCTIONS+=("$1"); }
_run_cleanup() { for func in "${_CLEANUP_FUNCTIONS[@]}"; do "${func}" 2>/dev/null || true; done; }
setup_traps() { trap _run_cleanup EXIT; trap 'exit 1' INT TERM; }

# Rollback infrastructure for installation failures
declare -a ROLLBACK_STACK=()

rollback_add() {
    ROLLBACK_STACK+=("$*")
}

rollback_clear() {
    ROLLBACK_STACK=()
}

rollback_execute() {
    local exit_code=$?
    if [[ $exit_code -ne 0 && ${#ROLLBACK_STACK[@]} -gt 0 ]]; then
        log_warning "Operation failed, rolling back changes..."
        for ((i=${#ROLLBACK_STACK[@]}-1; i>=0; i--)); do
            log_info "Rollback: ${ROLLBACK_STACK[i]}"
            eval "${ROLLBACK_STACK[i]}" 2>/dev/null || true
        done
        log_success "Rollback completed"
    fi
    ROLLBACK_STACK=()
    return $exit_code
}

# Enable rollback trap (call at start of installation functions)
enable_rollback() {
    trap rollback_execute EXIT
}

# Last updated: 2026-01
