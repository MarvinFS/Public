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
# AMNEZIAWG 1.5 PARAMETERS
# ============================================================================

AWG_JC=4; AWG_JMIN=6; AWG_JMAX=18
AWG_I1="<b 0xbc241120484e1a4b24a07468080045000000588e400080060000c0a802079570700bf0f701bb6acc450d92c8fc4e501800ffc831000016030107670100076303032da783e5f3ef7ab8284b63e766f44eb8a88366f9ed50474dc8f75c7c8a63d6e620872eb1968978f2aad21d8067cdc10fc76ff258513e8ed522eae13db943a67c620022130113031302c02bc02fcca9cca8c02cc030c00ac009c013c014009c009d002f0035010006f800000014001200000f646e7331312e71756164392e6e657400170000ff01000100000a0010000e11ec001d00170018001901000101000b00020100002300000010000e000c02683208687474702f312e310005000501000000000022000a00080403050306030203001200000033052f052d11ec04c0710a8630261b68009c2bd6a10075bf0955830072be633090d970953cd1b6d099081a79cd6c436fe7627ee5b03782e8237075ac173661251c110090cab78c72824261d69889cdcaa59e880072384c0e9880c3cb26da758af6b211c227b3a5dbc199a191051a604e1c4f0ef1b74312a643b636b507692a2c80f1fb6a9866a0faf2900b053899f549802085ea796b31410363f1a0c3d73aaf76527d8243cce614404bbed8861c4cb18e54c505eb141a70228d8ea6c16aa8669ed4478b0482db269a8964860308b2e5c4c89acba57f36369810669b83a16b11547d7b32abd214630459ff6c09c907008afb3fe84914c5ea61080482e51a8d5e70aa9f2a13a06966a0aa05d6c2ab374a436d53905c8a582aa00d6bdb2fe7d5bee2c43f03d1bf3ff927a8e653a0d7907563bb68033a055230ab1c896441815f86286d2929ff3aae84b82b8e072830e9296a77b9a842b10922ab9641b16ad23655f4baae89a1e171851dd891da491344e18ff9437018593cc75cb3a872bffb217e82dbb6f4b7ad28605cfde792cab862d9e3b6e1d639f5b70fb02045a2022fce69b467f0c7aa479ca47146720b08769c52c7d259e4059c8cfa6270eb1b188407d1f253da951462022424638e307c4414e93614f0770fa77a314c14d8e73eef15146d25404304371cc516d620b01375bd18410eaf3927eb08a4808cbaa5b5486df51fef54444dc205f960a0abab1768920ffa99b60a36701e6b4d5d3ace23b2cd7bf3a30196bbca72c57c62276203690ab3b89ba9b8934a4016a7b37336854e449e79104c70f87c197b5fe902a51deb2779d02f5b1a7d8b141a68818285a7a10333794923928906406c4537b3964db6bcb37ba64a4c2cc618141134489dd793c5bd78992d158f2c3b4ff92843b646920fc16e35c51ae819cf498897665153241149ef7ab13be08ee1ac1fb0691a4bd23df71c713d861a0e27864a296c42ec0783c8a5e84617b82b144a3530c5c17a54597026d79473bc998e72baebd604f4975c797c47bdba7da81656d3aaab06967dc8743a25f3cbe1e597b54c2a401c92657b3473d111a074894cd9ce191c115879bb7512a2f27576d4810adce7222a054db3c670787851edf11c20da584d4132d21a8e0521bb5aba122d87ccd5ccb8ae1b4cbfa0b7b94cb0eb342fdddc2d89d2ad9d483bc54706c078ac53db0327f14c6a855be733161d33566b66c3def3048537679611b1284a953a38618c29586f155f31966a2c0cbefa3c8ecd9832658049964cab4c20ce6050266d284e64c30edb835adeb75736101360b044007546af75b893c93f41588553d5c1d432a91a67b08161afbe44891a43adaf8a8634483c224b18f8481b0f380bb8a56c23fc979d41b1d8c8c5647b57ea6ca1c0435fadc2a65af09ac3124454e71ff8e97b33555de2d0adba090b7efa6c22b171692b4a8a9b33a1b700724330a59a7c0aa2b378e1ca5c060da309ce31e2231649bd77069a35bc7545514d12f909e015a27002cdc5d1cf9e310315174880a6ca2f3813e6051c796273518cbc64e488fed83d69f370a52b938df9217612ca1841b0ce979c464503c28213dd329ed2943618c1211cb4663c53378dfa3f6ec5682786734685f7ce7231acf51974ee0eede75da7546ac3a5023d5175e44e0a671c1bf649b74925dbc18a20e45b7d80962da422116d0a62fe4927a6b2bcc6b8380be5351c001d0020b74925dbc18a20e45b7d80962da422116d0a62fe4927a6b2bcc6b8380be5351c00170041047474cb68a651f1ab7626b8fa4d46e04010715ad5379b0433115586f7495612b4a4a7eb7eff6fac93de09d57fdb00cfcc43f99eec42a9e587fd4f1862732d7bcc002b00050403040303000d0018001604030503060308040805080604010501060102030201002d00020101001c00024001001b000706000100020003fe0d01190000010003cf00208babf77ac4db5952aaf591f65196c71a8d66728af77fa9e74d94009617d8e96500eff7ffb1d657a83256ca97d50af9ef5f7d03962987eb3356efac03e78f73018d1c4d91c97b500b6f6355f6351dd839558cb1c06915104ddffcf5276fa3a8afb512f7639ab71464a9eb20e24cbb35b4ffcfc9de5c3f1f6e47f3e3bd680b57aae5c5fff3c134e30eabf072168e452d76210210892be0c9671053cdcd2352e7758f8d872ea3b8a80c32198d066ec197c3d9bb052800acf96934d6e0de3f09981d729030b21591057cc30a62155a6a050a041ef20b14bfb700c6d5312144fb0d0546eba062d610cfba70395e596f8a0425389b7a6413a9417646c691a41fa6beca04e8cf75d403d33138b2333fb735973335>"
AWG_I2=0; AWG_I3=0; AWG_I4=0; AWG_I5=0
AWG_MTU=1320

[[ -f /etc/wireguard/awg-params ]] && source /etc/wireguard/awg-params

export AWG_JC AWG_JMIN AWG_JMAX AWG_I1 AWG_I2 AWG_I3 AWG_I4 AWG_I5 AWG_MTU

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
