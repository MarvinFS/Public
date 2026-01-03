# OpenWrt Configuration & Automation Scripts

Configuration scripts and guides for OpenWrt routers with Xray transparent proxy (VLESS + REALITY), WireGuard VPN, and automated geodata updates.

**Target**: GL.iNet GL-MT6000 (Flint 2) running OpenWrt 24.10.5 but can be used for any capable hardware.

```

## Automation Scripts

### [dns-speed.sh](dns-speed.sh)
Tests DNS-over-HTTPS endpoint latency for multiple providers (Cloudflare, Quad9, Google, doh.sb). Measures connection time, DNS query time, and provides color-coded statistics to help select the fastest upstream DNS for Xray configuration.

### [runetfreedom-geodata-updater.sh](runetfreedom-geodata-updater.sh)
Downloads and updates geoip.dat and geosite.dat files from RuNetFreedom's russia-v2ray-rules-dat repository. Performs SHA256 verification, atomic file replacement with timestamped backups, validates Xray config, and automatically rolls back on failure. Integrates with OpenWrt syslog for monitoring.

### [wg-add-client.sh](wg-add-client.sh)
Interactive WireGuard VPN client manager for adding, listing, and deleting VPN clients. Automatically assigns sequential IP addresses in 10.7.0.0/24 (IPv4) and fd00:7::/64 (IPv6) networks, generates client configuration files and QR codes for easy mobile device setup.

### [wg-rebuild-server-and-all-clients.sh](wg-rebuild-server-and-all-clients.sh)
WireGuard rebuild server keys and all keys for peers

### [argon_theme_english.sh](argon_theme_english.sh)
Argon theme installer. Interactive menu for installing and managing the Argon theme for LuCI web interface. Pre-built packages are for OpenWrt 24.10.5 - for newer versions use packages from original GitHub repositories. Includes wget availability checks and comprehensive error handling.
**Source**: Taken from "Flint 2 Community (ONLY IN RUSSIAN!!!): configuration help, support, ready-made scripts and secret features." 👉 https://t.me/flint_2

### [check-xray.sh](check-xray.sh) + [install-xray-health-widget.sh](install-xray-health-widget.sh)
Automated health monitoring and failover system for Xray tunnels. Continuously monitors tunnel health, automatically fails over to direct internet when tunnel is unreachable (ISP outages, server down, blocked), and auto-restores when tunnel recovers. Features intelligent diagnostics (distinguishes ISP outages from tunnel failures), persistent state across reboots, adaptive retry intervals and LuCI web interface integration with dual status display (Xray Tunnel + ISP Internet).
**Documentation**: [README-check-xray.md](README-check-xray.md)

## Documentation

### [openwrt_xray_vless_reality_how_to.md](openwrt_xray_vless_reality_how_to.md)
Complete step-by-step guide for configuring Xray transparent proxy with VLESS + REALITY protocol on OpenWrt. Covers LuCI web interface configuration, geographic routing (Russia direct, blocked content via proxy), TCP/UDP transparent proxy setup, DNS integration, and comprehensive troubleshooting procedures.

### [luci-app-xray-compile-guide.md](luci-app-xray-compile-guide.md)
Universal compilation guide for building luci-app-xray packages using OpenWrt SDK on Debian 12. Includes router detection, SDK setup, compilation steps, and package installation instructions.

### [readme_runetfreedom_geodata_updater.md](readme_runetfreedom_geodata_updater.md)
Detailed documentation for the RuNetFreedom geodata updater script. Covers installation, cron job setup for automatic updates, rollback procedures, and integration with Xray routing rules for geographic-based traffic routing.

### [README-check-xray.md](README-check-xray.md)
Comprehensive documentation for the Xray health monitoring and automated failover system. Covers why it was created, architecture and operation logic, step-by-step installation, usage commands (manual restore, force restore), logging, etc.

### [xray_UDP_transparent_proxy.md](xray_UDP_transparent_proxy.md)
Advanced guide for enabling UDP transparent proxy with port-based routing for VoIP applications (Telegram, WhatsApp, Discord, FaceTime). Explains DNS protection with bypass rules, custom configuration hooks using ucode, and debugging procedures for UDP traffic routing.

### [WireGuard_Install_OpenWrt_24.10.md](WireGuard_Install_OpenWrt_24.10.md)
Installation and configuration guide for WireGuard VPN on OpenWrt 24.10+. Covers package installation, server and client configuration, firewall rules, NAT setup, and IPv4/IPv6 dual-stack configuration.

## Quick Start: Download and Run Scripts

### Prerequisites
Ensure `curl` or `wget` is installed on your OpenWrt device:
```bash
opkg update && opkg install curl wget
```

### Download script and Execute on the router without 3rd party software.
You may obivously use any other method scp on Linux, or WinSCP on Windows to connect to your router and tranfer files around.
Here is the simplest direct route.
Download any script from this list:

dns-speed.sh
runetfreedom-geodata-updater.sh
wg-add-client.sh
wg-rebuild-server-and-all-clients.sh
check-xray.sh (requires install-xray-health-widget.sh for LuCI integration)

directly from GitHub, make it executable, and run:

```bash
# Download using curl
curl -fsSL https://raw.githubusercontent.com/MarvinFS/Public/main/openwrt/<script-name>.sh -o /root/script.sh && chmod +x /root/script.sh
# Run
/root/script.sh

# OR download using wget
wget -O /root/script.sh https://raw.githubusercontent.com/MarvinFS/Public/main/openwrt/<script-name>.sh && chmod +x /root/script.sh 

#Run script
/root/script.sh
```

**Storage Location**: Replace `/root/` with your preferred directory:
- `/root/` - Persistent storage in root home directory
- `/usr/local/sbin/` - For system-wide utility scripts - but it must be added to the stock backup system first and the directory itself must be also manually created. 
- `/tmp/` - Temporary storage (cleared on reboot, useful for one-time use)

---
**Last Updated**: 2026-01-02
