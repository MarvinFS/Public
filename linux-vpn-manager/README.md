# Linux VPN Manager

**One-liner install on stock Linux** that deploys production-ready VPN with zero manual configuration.

## What It Does

- **Installs & configures 6 VPN protocols**:
  - XRay XHTTP+Nginx – Decoy website with hidden VPN tunnel, strongest anti-censorship (requires domain)
  - XRay CDN Tunnel – Traffic routed through Cloudflare CDN (requires domain + CF account)
  - XRay VLESS+REALITY – Direct obfuscation, mimics real TLS (no domain needed)
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
curl -fsSL https://raw.githubusercontent.com/MarvinFS/Public/main/linux-vpn-manager/{vpn-manager,common,wireguard,openvpn,shadowsocks,xray,xhttp,xhttp-nginx}.sh -O
chmod +x *.sh

# Run the manager
sudo ./vpn-manager.sh
```

**Using wget** (if curl not available):
```bash
mkdir -p /opt/vpn-manager && cd /opt/vpn-manager
for f in vpn-manager common wireguard openvpn shadowsocks xray xhttp xhttp-nginx; do
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
| `xhttp.sh`       | XRay CDN Tunnel (gRPC via Cloudflare) install + multi-user management
| `xhttp-nginx.sh` | XRay XHTTP+Nginx (decoy website) install + multi-user management
| `install.sh`        | Used only when installing with one-liner

## Features

### Traffic Obfuscation

- **XRay XHTTP+Nginx** - Strongest anti-censorship (2026)
  - Nginx reverse proxy with real Let's Encrypt certificate
  - Decoy website (game server log aggregation) served to casual visitors and DPI probes
  - VPN traffic hidden on a specific path via XHTTP stream-up transport
  - Nginx `grpc_pass` forwards to xray on a Unix socket
  - Compatible with v2rayNG, v2rayN, NekoBox, Hiddify

- **XRay VLESS+REALITY** - Best-in-class direct obfuscation
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

### When to use which protocol

Direct VLESS+REALITY (xray.sh) is faster and simpler - no domain needed, no CDN config, full line speed. Use it when it works.

CDN Tunnel (xhttp.sh) routes through Cloudflare so censors only see a connection to Cloudflare's IP. Requires a domain on Cloudflare. Adds 30-80ms latency from CDN routing. Use it when REALITY gets blocked (e.g. Russia's TSPU fingerprinting REALITY handshakes in 2026).

Both can run simultaneously on the same server - different services, different ports, different clients.

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

