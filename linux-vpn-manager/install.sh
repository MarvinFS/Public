#!/bin/bash
#
# Linux VPN Manager - Bootstrap Installer
# Downloads all required scripts and launches the manager
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager/install.sh | sudo bash
#   wget -qO- https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager/install.sh | sudo bash
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

BASE_URL="https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager"
INSTALL_DIR="/opt/vpn-manager"

SCRIPTS=(
    "vpn-manager.sh"
    "common.sh"
    "wireguard.sh"
    "openvpn.sh"
    "shadowsocks.sh"
    "xray.sh"
)

echo ""
echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Linux VPN Manager - Installer        ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
echo ""

# Check root
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR] This script must be run as root (sudo)${NC}"
    exit 1
fi

# Detect download tool
if command -v curl &>/dev/null; then
    DOWNLOAD="curl -fsSL"
    DOWNLOAD_OUT="-o"
elif command -v wget &>/dev/null; then
    DOWNLOAD="wget -qO"
    DOWNLOAD_OUT=""
else
    echo -e "${RED}[ERROR] Neither curl nor wget found. Install one first:${NC}"
    echo "  apt install curl    # Debian/Ubuntu"
    echo "  dnf install curl    # AlmaLinux/Rocky"
    exit 1
fi

# Create install directory
echo -e "${CYAN}>>> Installing to ${INSTALL_DIR}${NC}"
mkdir -p "${INSTALL_DIR}"
cd "${INSTALL_DIR}"

# Download all scripts
echo -e "${CYAN}>>> Downloading scripts...${NC}"
for script in "${SCRIPTS[@]}"; do
    echo -n "    ${script}... "
    if [[ -n "${DOWNLOAD_OUT}" ]]; then
        # curl: -f fails on HTTP errors, -o outputs to file
        if ${DOWNLOAD} "${BASE_URL}/${script}" ${DOWNLOAD_OUT} "${script}"; then
            echo -e "${GREEN}OK${NC}"
        else
            echo -e "${RED}FAILED${NC}"
            echo -e "${RED}[ERROR] Failed to download ${script}${NC}"
            echo -e "${RED}URL: ${BASE_URL}/${script}${NC}"
            exit 1
        fi
    else
        # wget: -qO outputs to file
        if ${DOWNLOAD} "${script}" "${BASE_URL}/${script}"; then
            echo -e "${GREEN}OK${NC}"
        else
            echo -e "${RED}FAILED${NC}"
            echo -e "${RED}[ERROR] Failed to download ${script}${NC}"
            echo -e "${RED}URL: ${BASE_URL}/${script}${NC}"
            exit 1
        fi
    fi
done

# Make executable
chmod +x *.sh

# Create symlink for easy access (/usr/bin is always in PATH, /usr/local/bin might not be)
ln -sf "${INSTALL_DIR}/vpn-manager.sh" /usr/bin/vpn-manager 2>/dev/null || \
    ln -sf "${INSTALL_DIR}/vpn-manager.sh" /usr/local/bin/vpn-manager 2>/dev/null || true

echo ""
echo -e "${GREEN}[SUCCESS] Installation complete!${NC}"
echo ""
echo -e "  Location: ${CYAN}${INSTALL_DIR}${NC}"
echo -e "  Command:  ${CYAN}vpn-manager${NC} (or ${CYAN}sudo ${INSTALL_DIR}/vpn-manager.sh${NC})"
echo ""

# Ask to launch
read -rp "Launch VPN Manager now? [Y/n]: " launch
launch=${launch:-Y}

if [[ "${launch}" =~ ^[Yy]$ ]]; then
    echo ""
    exec "${INSTALL_DIR}/vpn-manager.sh"
fi
