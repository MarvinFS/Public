# Linux VPN Manager

**One-liner install on stock Linux** that deploys production-ready VPN with zero manual configuration.

## What It Does

- **Installs & configures 4 VPN protocols**:
  - XRay VLESS+REALITY – Best-in-class obfuscation (no domain needed, mimics real TLS)
  - WireGuard – Fast modern VPN (supports AmneziaWG 1.5 client config export)
  - OpenVPN – Classic SSL VPN
  - Shadowsocks – Encrypted proxy - pretty much outdated already
  
- **Auto-optimizes server**:
  - BBR congestion control
  - TCP buffer tuning for throughput
  - IP forwarding & firewall rules
  - sysctl network optimizations
  - auto MSS clumping and much more...

- **Comprehensive user management** (menu-based):
  - Add / remove / list clients
  - Export client configs (files, QR codes, share links)
  
Target: Server owners who want to deploy VPN and forget - zero manual config needed.

## Prerequisites

- **Root access** required (sudo)
- **Supported OS**: Ubuntu 22.04+, Debian 10+, AlmaLinux/Rocky 9+
- **Network**: Public IP or port forwarding configured, Static works fine, dynamic IP needs some ddns fqdn working already (google that)

## Quick Start

### One-Line Install (Recommended)

```bash
# Using curl
curl -fsSL https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager/install.sh | sudo bash

# Using wget (for minimal systems)
wget -qO- https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager/install.sh | sudo bash
```

This downloads all scripts to `/opt/vpn-manager/` and creates the `vpn-manager` command.

### Manual Download

```bash
# Download all scripts at once
mkdir -p /opt/vpn-manager && cd /opt/vpn-manager
curl -fsSL https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager/{vpn-manager,common,wireguard,openvpn,shadowsocks,xray}.sh -O
chmod +x *.sh

# Run the manager
sudo ./vpn-manager.sh
```

**Using wget** (if curl not available):
```bash
mkdir -p /opt/vpn-manager && cd /opt/vpn-manager
for f in vpn-manager common wireguard openvpn shadowsocks xray; do
  wget -q "https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager/${f}.sh"
done
chmod +x *.sh
sudo ./vpn-manager.sh
```
---

## Overview

| Script | Purpose |
|--------|---------|
| `vpn-manager.sh` | Main entry point - orchestrates all VPN operations
| `common.sh`      | Shared library - logging, OS detection, optimizations 
| `wireguard.sh`   | WireGuard install + client management
| `shadowsocks.sh` | Shadowsocks-rust install + management
| `openvpn.sh`     | OpenVPN install + client management
| `xray.sh`        | XRay VLESS+REALITY install + multi-user management
| `install.sh`        | Used only when installing with one-liner

## Features

### Traffic Obfuscation

- **XRay VLESS+REALITY** - Best-in-class obfuscation (end of 2025)
  - No domain or TLS certificate required
  - Traffic mimics legitimate TLS to real websites (default: `browser.yandex.com`)
  - Multi-user support with unique shortIds per client (for tracking/revocation)
  - Compatible with **AmneziaVPN** client (Windows/Android/iOS)
  - Generate VLESS share links and QR codes

### Performance Optimizations (built-in)

- BBR congestion control
- TCP buffer tuning
- Connection tracking limits
- MSS clamping for MTU issues
- sysctl network optimizations

#### Verify Optimizations Applied

```bash
# Check BBR is active
sysctl net.ipv4.tcp_congestion_control
# Expected: net.ipv4.tcp_congestion_control = bbr

# Check IP forwarding
sysctl net.ipv4.ip_forward
# Expected: net.ipv4.ip_forward = 1

# Check TCP buffers
sysctl net.ipv4.tcp_rmem net.ipv4.tcp_wmem
# Expected: 4096 1048576 16777216

# Check all VPN optimizations at once
cat /etc/sysctl.d/99-vpn-optimizations.conf

# Check MSS clamping rules
iptables -t mangle -L FORWARD -n -v | grep TCPMSS
```

## Supported Operating Systems

| Distribution | Versions |
|-------------|----------|
| Ubuntu | 20.04, 22.04, 24.04+ |
| Debian | 10, 11, 12+ |
| AlmaLinux | 8, 9+ |
| Rocky Linux | 8, 9+ |

## Usage

### Main Menu (`vpn-manager.sh`)

```
╔════════════════════════════════════════════════════════════╗
║          Linux VPN Server Manager v6.0                     ║
║    WireGuard • OpenVPN • Shadowsocks • XRay                ║
╚════════════════════════════════════════════════════════════╝

Service Status:

  WireGuard:   Not installed
  OpenVPN:     Not installed
  Shadowsocks: Not installed
  XRay:        Not installed

Install / Manage:

  1) WireGuard      - Fast, modern VPN
  2) OpenVPN        - Battle-tested VPN
  3) Shadowsocks    - Lightweight proxy
  4) XRay           - VLESS+REALITY (best obfuscation)
```

### XRay VLESS+REALITY

```bash
# XRay management menu
sudo ./xray.sh

# Options:
# 1) Add client
# 2) List clients  
# 3) Show client config & QR
# 4) Revoke client
# 5) Change port
# 6) Regenerate keys
# 7) Show status
# 8) Restart service
# 9) Uninstall
```

### Individual Module Usage

Each VPN module auto-detects whether the VPN is installed:
- **If not installed** → Shows installation wizard
- **If installed** → Shows management menu

## Default Ports

| Service | Port | Protocol |
|---------|------|----------|
| WireGuard | 51820 | UDP |
| OpenVPN | 1194 | UDP |
| Shadowsocks | 8388 | TCP+UDP |
| XRay | 443 | TCP |

## Client Setup

See [docs/CLIENT_SETUP.md](docs/CLIENT_SETUP.md) for:
- QR code scanning for WireGuard
- Importing `.conf` files
- Shadowsocks configuration
- OpenVPN `.ovpn` import
- XRay VLESS+REALITY with AmneziaVPN

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for common issues:
- Connection timeouts
- DNS resolution failures
- Permission errors
- Service startup issues

### FYI considerations - Why we use direct REALITY (and Not XHTTP)

| Feature | Direct REALITY ✅ | CDN Fronting (XHTTP) |
|---------|------------------|----------------------|
| Latency | Server RTT only (~20-50ms) | +40-100ms overhead |
| Download | Full line speed | ~50-70% of direct |
| Upload | Full line speed | **~10-50 Mbps max** |
| Setup | Single server, no domain | Requires domain + CDN config |
| Idle timeout | None | CF kills after 100 sec |
| Protocol | `xtls-rprx-vision` (optimized) | HTTP wrapping overhead |

**We use direct VLESS+REALITY because:**

- **Performance** - Direct connection = fastest speed, lowest latency
- **Simplicity** - No domain, no CDN account, no extra configuration  
- **Reliability** - No middleman timeouts or rate limits
- **Already obfuscated** - Traffic looks like legitimate TLS to `browser.yandex.com`

**CDN fronting (XHTTP) is only useful when:**

- Your server IP is actively blocked
- You're behind strict corporate/national firewalls
- Direct connections fail completely

> **Rule of thumb:** Direct REALITY = highway (fast). CDN fronting = detour (slow, last resort).

## License

MIT License - See LICENSE file for details.

## Credits

- **WireGuard installer** based on [wireguard-install](https://github.com/angristan/wireguard-install) by angristan

## Contributing

1. Fork the repository
2. Create a feature branch
3. Test on multiple distributions
4. Submit a pull request

---

