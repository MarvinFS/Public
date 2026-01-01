# OpenWrt Xray (VLESS + REALITY) Complete Setup Guide

> **Router:** GL.iNet GL-MT6000 (Flint 2)
> **OpenWrt:** 24.10.5 (mediatek/filogic) - kernel 6.6.119
> **Xray core:** openwrt-xray 25.12.8-1 (arm64)
> **LuCI integration:** luci-app-xray 3.6.1-1 + luci-app-xray-status 3.6.1-1

---

## Table of Contents

1. [Overview](#1-overview)
2. [Lessons Learned](#2-lessons-learned)
3. [Prerequisites](#3-prerequisites)
4. [Install Xray Core Binary](#4-install-xray-core-binary)
5. [Install GeoIP and GeoSite Datasets](#5-install-geoip-and-geosite-datasets)
6. [Building LuCI Apps from Source](#6-building-luci-apps-from-source)
7. [Install LuCI Apps](#7-install-luci-apps)
8. [LuCI Configuration](#8-luci-configuration)
9. [Service Management](#9-service-management)
10. [Connectivity Testing](#10-connectivity-testing)
11. [Troubleshooting Playbook](#11-troubleshooting-playbook)
12. [Appendices](#appendices)

---

## 1. Overview

This guide covers transparent proxy setup using Xray with VLESS + REALITY protocol on OpenWrt. The configuration enables:

- **TCP transparent proxy** for all HTTP/HTTPS traffic
- **UDP transparent proxy** with proper port bypass rules (for VoIP apps)
- **DNS integration** via Xray DNS listeners injected into dnsmasq
- **Geographic routing** - in my case Russian internal websites traffic routed directly, all other content via proxy

### Related Documentation

| Document | Purpose |
|----------|---------|
| [readme_runetfreedom_geodata_updater.md](readme_runetfreedom_geodata_updater.md) | RuNetFreedom geodata setup, DNS configuration, routing rules |
| [xray_UDP_transparent_proxy.md](xray_UDP_transparent_proxy.md) | Advanced UDP proxy for VoIP apps (Telegram, WhatsApp, Discord) |

---

## 2. Lessons Learned

1. **UDP without proper bypass rules** - enabling UDP interception without bypassing port 53 (DNS) causes DNS breakage
2. **Proxying router's own DNS** (AdGuardHome upstream DoH endpoints) caused self-dependency and timeouts - had to abandon AdGuard on the router, unfortunately (probably would work in a container)
3. **DoH without specific format `https+local://`** - causes DNS routing loop and nothing works
4. **Server Hostname Resolution (FQDN)** - If your Xray server uses FQDN (not static IP), you **must** configure "Server Hostname Resolving" in the server profile. Use a direct DNS server (e.g., `5.141.95.250`) via UDP method. Without this, Xray tries to resolve the server hostname through its own DNS routing, creating a recursion loop. Configure in: **Services → Xray → General Settings → [Your Server] → Server Hostname Resolving tab**. See [readme_runetfreedom_geodata_updater.md](readme_runetfreedom_geodata_updater.md#61-configure-remote-xray-server-hostname-resolution-if-using-fqdn) for details.
5. **AdGuardHome with Xray:** Currently problematic due to dnsmasq limitations in binding to IP addresses and DHCP. May be resolved in OpenWrt 25 which separates dnsmasq into two services.
6. **Secure LuCI Access:** Use SSH tunnel for secure remote access: [OpenWrt SSH Tunnel Guide](https://openwrt.org/docs/guide-user/luci/luci.secure#setting_up_the_ssh-tunnel)

### Related Guides

- For detailed UDP configuration, see [xray_UDP_transparent_proxy.md](xray_UDP_transparent_proxy.md)
- For detailed routing configuration, see [readme_runetfreedom_geodata_updater.md](readme_runetfreedom_geodata_updater.md)
---

## 3. Prerequisites

### 3.1 Router Baseline

- Working WAN and LAN connectivity
- LAN gateway: `192.168.1.1` (if different need to change this document)
- LAN bridge interface: `br-lan`

### 3.2 Essential Packages

```sh
opkg update
opkg install curl ca-bundle ss drill ca-certificates nano openssh-sftp-server wget 
```
---

## 4. Install Xray Core Binary

Download the IPK from the [openwrt-xray releases](https://github.com/yichya/openwrt-xray/releases):

```sh
cd /tmp
uclient-fetch -O openwrt-xray.ipk \
  "https://github.com/yichya/openwrt-xray/releases/download/v25.12.8/openwrt-xray_25.12.8-1_aarch64_cortex-a53.ipk"
```

```sh
opkg install ./openwrt-xray.ipk
```

```sh
xray version 2>/dev/null || /usr/bin/xray version
```

Expected output: `Xray 25.12.8 ... linux/arm64`

---

## 5. Install GeoIP and GeoSite Datasets

These datasets enable routing rules like `geoip:RU` and `geosite:category-ads`:

```sh
opkg install v2ray-geoip v2ray-geosite
```

Verify installation:

```sh
ls -la /usr/share/v2ray/
# Expected: geoip.dat and geosite.dat
```

### 5.1 RuNetFreedom Enhanced Geodata

For Russia-specific blocking bypass with auto-updates, see the dedicated guide:
**[readme_runetfreedom_geodata_updater.md](readme_runetfreedom_geodata_updater.md)**

Key categories available:
- `geosite:ru-blocked` - Blocked domains in Russia
- `geosite:ru-available-only-inside` - Domains accessible only inside Russia
- `geosite:category-ads-all` - All advertising domains
- All standard v2fly categories (google, discord, youtube, etc.)

---

## 6. Building LuCI Apps from Source

Building the LuCI apps requires compiling them specifically for your OpenWrt version and architecture.

### 6.1 Using GitHub Actions (Recommended)

Manual compilation on Ubuntu 24.04 proved extremely difficult due to dependency issues. After wasting almost a day trying to fight with all the needed dependencies, I quit and compiled the app using **GitHub Actions** (as guided by the original repository) in 30-60 mins, depending you the time of the day and your level of subscription.

1. **Fork the repository:**
   - Go to [luci-app-xray](https://github.com/yichya/luci-app-xray)
   - Click **Fork** to create your own copy

2. **Configure GitHub Actions:**
   - Navigate to your fork's **Actions** tab
   - Enable workflows if prompted
   - The repository includes pre-configured workflows for building IPKs

3. **Trigger a build:**
   - Go to **Actions** → Select the build workflow
   - Click **Run workflow**
   - Select your target OpenWrt version and architecture

4. **Download artifacts:**
   - Once the build completes, download the IPK files from the workflow artifacts
   - Alternatively, create a GitHub Release and attach the IPKs

### 6.2 Alternative: OpenWrt SDK (Advanced)

If you prefer local builds, use the OpenWrt SDK in a dedicated environment (possibly a dedicated OpenWrt build VM - not a regular Ubuntu VM, which might have been my mistake):

1. **Download SDK** matching your router:
   - `openwrt-sdk-24.10.5-mediatek-filogic_gcc-13.3.0_musl.Linux-x86_64`

2. **Extract and configure:**
   ```sh
   tar -xf openwrt-sdk-24.10.5-mediatek-filogic_*.tar.*
   cd openwrt-sdk-24.10.5-mediatek-filogic_*
   ./scripts/feeds update -a
   ./scripts/feeds install -a
   ```

3. **Add package source:**
   ```sh
   cd package
   git clone https://github.com/yichya/luci-app-xray.git
   cd ..
   ```

4. **Select and build:**
   ```sh
   make menuconfig
   # Select: LuCI → Applications → luci-app-xray = m
   # Select: LuCI → Applications → luci-app-xray-status = m
   make package/luci-app-xray/compile V=s
   ```

5. **Find artifacts:**
   - IPKs are in `bin/packages/aarch64_cortex-a53/luci/`

---

## 7. Install LuCI Apps
Use WinSCP or any SFTP client:
Connect to `192.168.1.1` over SFTP
Upload IPKs to `/tmp/`

```sh
opkg install /tmp/luci-app-xray_3.6.1-1_all.ipk
opkg install /tmp/luci-app-xray-status_3.6.1-1_all.ipk
```
- That also installs `kmod-nf-tproxy` and `kmod-nft-tproxy`
- Creates init script: `/etc/init.d/xray_core`
- Auto-generates configs in `/var/etc/xray/`
- Injects dnsmasq config pointing to `127.0.0.1#5300-5303`

### 7.1 Verify Installation

```sh
ls -l /etc/init.d | grep xray
/etc/init.d/xray_core status
ps | grep '[x]ray'
```

---

## 8. LuCI Configuration

Access LuCI at `http://192.168.1.1/cgi-bin/luci` and navigate to **Services → Xray**.

### 8.1 General Settings Tab

#### Transparent Proxy
- **Enable Transparent Proxy:** ✅ Enabled

#### TPROXY Interfaces
- **Transparent interfaces IPv4:** `br-lan`
- **IPv6:** Optional (configure if using IPv6)

#### Bypass Interfaces
- **IPv4:** `eth1`, `pppoe-wan`
- **IPv6:** `eth1`, `pppoe-wan`

#### Strategy Settings
- **Balancer strategy:** `random`
- **Domain strategy:** `IPIfNonMatch`

#### Logging
- **Log level:** `warning`
- **Access log:** I disabled (optional)
- **DNS log:** I also disabled (useful for troubleshooting)

#### TCP/UDP Server Selection

| Setting | Value | Notes |
|---------|-------|-------|
| TCP Server (IPv4) | Select your server profile | Enables TCP proxying |
| TCP Server (IPv6) | Optional / Direct | Only if you configure IPv6 |
| UDP Server (IPv4) | Select your server profile | Enables UDP proxying for VoIP |

**Important:** When enabling UDP, you **must** configure bypass ports. See Section 8.4.

### 8.2 Xray Servers Tab (Outbound Profile)

Create a server entry for your VLESS + REALITY connection:

| Field | Value |
|-------|-------|
| Alias | `anything` |
| Protocol | `vless` |
| Server | `FQDN` |
| Port | `443` |
| UUID/Password | `UUID from config` |
| VLESS encryption | `none` |
| TLS/Security | `reality` |
| Flow | `xtls-rprx-vision` |
| Fingerprint | `chrome` |
| ServerName (SNI) | `any public service FQDN you want` |
| PublicKey | `*<key here>*` |
| ShortId | `ID` |
| Transport | `tcp` |
| TCP guise/header | `none` |
| Domain strategy | `UseIP` |
| Domain resolve DNS method | `udp` |

### 8.3 DNS Tab

**Understanding DNS Rules:**

The DNS settings are **not** a smart fallback system. They are simple rule-based buckets:

```
if domain matches Blocked rules:
    return NXDOMAIN or loopback

else if domain matches Bypassed domain rules:
    resolve via Fast DNS

else if domain matches Forwarded domain rules:
    resolve via Secure DNS

else:
    resolve via Default DNS
```

#### My production Settings 

| Setting | Value |
|---------|-------|
| **Fast DNS** | `5.141.95.250` |
| **Bypassed domain rules** | `domain:<remoteXRAYFQDN>`<br>`domain:doh.sb`<br>`domain:cloudflare-dns.com`<br>`geosite:category-ru`<br>`geosite:ru-available-only-inside` |
| **Secure DNS** | `8.8.8.8:53` |
| **Forwarded domain rules** | `domain:google.com`<br>`geosite:category-ai-!cn`<br>`geosite:google` |
| **Default DNS** | `1.1.1.1:53` |
| **Blocked domain rules** | `geosite:category-ads-all` |
| **Blocked to loopback** | Unchecked (returns NXDOMAIN) |
| **Xray DNS Server Port** | `5300` |

**Why these bypassed domains:**
- `domain:<remoteXRAYFQDN>` - Xray server FQDN (must resolve directly, not through proxy)
- `domain:doh.sb`, `domain:cloudflare-dns.com` - DoH bootstrap domains (prevent DNS loops)
- `geosite:category-ru` - Russian services (Yandex, Mail.ru, VK, etc.)
- `geosite:ru-available-only-inside` - Sites only accessible from Russia

**Why these forwarded domains:**
- `geosite:category-ai-!cn` - AI services (OpenAI, Anthropic, etc.) excluding Chinese
- `geosite:google` - All Google services
- `domain:google.com` - Explicit Google domain
- `all the rest proxied` - which is not covered by the above

#### DoH Configuration Warning

**Critical:** If using DoH, specify it using `https+local://` format in custom options. Otherwise, DoH requests are routed through the tunnel, creating a DNS loop. I failed to get custom DoH array working correctly with DNS routing - using plain DNS servers instead.

For detailed DNS configuration, see [readme_runetfreedom_geodata_updater.md](readme_runetfreedom_geodata_updater.md#6-configure-xray-dns-routing-rules-optional-but-recommended).

### 8.4 Outbound Routing Tab

**Goal:** Route Russia direct, enable UDP with proper bypasses.

#### GeoIP Direct Code List
- **IPv4:** `RU`
- **IPv6:** `RU`

This generates routing rules like: `ip: ["geoip:RU"] -> outboundTag: dynamic_direct`

#### Bypass IPs
- `192.168.0.0/16` - Local LAN
- `127.0.0.0/8` - Loopback
- `::1/128` - IPv6 loopback
- Your Xray server's public IP (if static)

#### Bypass TCP Ports
- `853` - DNS over TLS
- `49665` - Custom (if needed)

#### Bypass UDP Ports (Critical for UDP Proxy)

| Port | Purpose | Why Bypass |
|------|---------|------------|
| `53` | DNS | **MUST** be bypassed - prevents DNS loops |
| `443` | QUIC/HTTP3 | Prevents routing loops |
| `123` | NTP | Time synchronization |

**Warning:** Do NOT mix overlapping intervals. If you have `192.168.0.0/16`, do NOT also add `192.168.1.1/32`.

### 8.5 LAN Hosts Access Control Tab

**Critical:** Bypass the router itself to prevent its services from being captured by transparent proxy:

| Field | Value |
|-------|-------|
| Alias | `firewall itself` |
| MAC | Your router's MAC address |
| IPv4 strategy | `bypass` |
| IPv6 strategy | `bypass` |

### 8.6 UDP Transparent Proxy for VoIP Apps

For detailed configuration of UDP proxy to enable voice/video calls in Telegram, WhatsApp, FaceTime, and Discord, see:

**[xray_UDP_transparent_proxy.md](xray_UDP_transparent_proxy.md)**

Key points:
- UDP proxy works when at least port 53 is bypassed
- VoIP apps use specific UDP port ranges that need to be routed through proxy, but we route all UDP so no need to be specific.
- Port reference table available in the linked document

---

## 9. Service Management

### 9.1 Check Status

```sh
/etc/init.d/xray_core status
```

### 9.2 Clean Restart Procedure

```sh
/etc/init.d/xray_core stop
killall -q xray 2>/dev/null || true
rm -rf /var/etc/xray/*
/etc/init.d/firewall restart
/etc/init.d/dnsmasq restart
/etc/init.d/xray_core start
```

### 9.3 Validate Configuration

```sh
/usr/bin/xray run -test -confdir /var/etc/xray | head -50
# Expected output: Configuration OK.
```

### 9.4 View Generated Config

```sh
sed -n '1,200p' /var/etc/xray/config.json
```

### 9.5 Monitor DNS Logs (if enabled)

```sh
logread -f -e 'app/dns'
```

---

## 10. Connectivity Testing

### 10.1 Verify Listening Ports

```sh
busybox netstat -lnptu 2>/dev/null | grep -E ':(5300|5301|5302|5303)\b|:(1080|1081|1082|1083|1084|1085)\b' || true
```

Expected:
- TCP 5300-5303 (DNS inbounds)
- TCP/UDP 1080-1089 (proxy inbounds, depending on configuration)

### 10.2 Verify Xray Process

```sh
ps | grep '[x]ray'
```

### 10.3 Test Direct vs Proxy Egress

**Direct connection (no proxy):**
```sh
curl -4 -s https://ipinfo.io/ip
```

**Through SOCKS proxy:**
```sh
curl -4 -s --socks5-hostname 127.0.0.1:1080 https://ipinfo.io/ip
```

### 10.4 Test DNS Resolution

**From router:**
```sh
nslookup google.com 1.1.1.1
```

**From Windows client:**
```bat
ping 192.168.1.1
nslookup google.com 192.168.1.1
```

### 10.5 DoH Latency Test Script

See [dns-speed.sh](dns-speed.sh) for a simple DoH endpoint latency testing script.

---

## 11. Troubleshooting Playbook

### 11.1 LAN Loses DNS After Configuration Change

1. Verify UDP bypass port `53` is configured in Outbound Routing
2. Verify router itself is bypassed in LAN hosts access control
3. Perform clean restart (Section 9.2)

### 11.2 Firewall Restart Fails with NFT Errors

**Symptom:** `Error: conflicting intervals specified`

**Fix:** Remove overlapping bypass IPs. Do not mix `192.168.0.0/16` with `192.168.1.1/32`.

### 11.3 Config Generation Issues

```sh
rm -rf /var/etc/xray/*
/etc/init.d/xray_core start
/usr/bin/xray run -test -confdir /var/etc/xray
```

### 11.4 VoIP Calls Not Working

See [xray_UDP_transparent_proxy.md](xray_UDP_transparent_proxy.md#troubleshooting) for detailed VoIP troubleshooting.

---

## 12. Appendices

### 12.1 Files and Paths

| Purpose | Path |
|---------|------|
| Init script | `/etc/init.d/xray_core` |
| Generated Xray config | `/var/etc/xray/config.json` |
| NFT include entrypoint | `/usr/share/nftables.d/table-pre/xray_core.nft` |
| NFT generated rules | `/var/etc/xray/*.nft` |
| RuNetFreedom geodata | `/usr/local/share/xray-assets/` |
| Geodata symlinks | `/usr/share/xray/` |

### 12.2 Useful CLI Commands

**Add SSH rule for WAN access:** (just a quick rules cleation from cli, not related to xray)
```sh
uci add firewall rule
uci rename firewall.@rule[-1]='wan_ssh_allow'
uci set firewall.wan_ssh_allow.name='Allow SSH from WAN'
uci set firewall.wan_ssh_allow.src='wan'
uci set firewall.wan_ssh_allow.proto='tcp'
uci set firewall.wan_ssh_allow.dest_port='2222'
uci set firewall.wan_ssh_allow.target='ACCEPT'
uci commit firewall
/etc/init.d/firewall reload
```

**Stop and fully reset Xray:**
```sh
/etc/init.d/xray_core stop
killall -q xray 2>/dev/null || true
rm -rf /var/etc/xray/*
/etc/init.d/dnsmasq restart
/etc/init.d/firewall restart
/etc/init.d/xray_core start
/etc/init.d/xray_core status
```

---

## References

- [Xray Routing Tutorial](https://xtls.github.io/Xray-docs-next/document/level-1/routing-lv1-part1.html)
- [Xray DNS Documentation](https://xtls.github.io/config/dns.html#dnsobject)
- [openwrt-xray Repository](https://github.com/yichya/openwrt-xray)
- [luci-app-xray Repository](https://github.com/yichya/luci-app-xray)
- [RuNetFreedom Geodata](https://github.com/runetfreedom/russia-v2ray-rules-dat)
- [v2fly Domain List Community](https://github.com/v2fly/domain-list-community)
- [Loyalsoldier v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat)
- [OpenWrt Secure LuCI Access](https://openwrt.org/docs/guide-user/luci/luci.secure#setting_up_the_ssh-tunnel)

## See Also

- [readme_runetfreedom_geodata_updater.md](readme_runetfreedom_geodata_updater.md) - RuNetFreedom geodata setup (recommended)
- [xray_UDP_transparent_proxy.md](xray_UDP_transparent_proxy.md) - Advanced UDP transparent proxy for VoIP apps
- [WireGuard_Install_OpenWrt_24.10.md](WireGuard_Install_OpenWrt_24.10.md) - WireGuard VPN setup
