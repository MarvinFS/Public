# Client Setup Guide

Last updated: 2026-01

This guide covers connecting to your VPN server from various platforms.

> **Part of Linux VPN Manager** - See [README.md](../README.md) for server side documentation.
 
## Project Structure

```
linux-vpn-manager/
├── vpn-manager.sh          # Main entry point
├── common.sh               # Shared library
├── wireguard.sh            # WireGuard install + management including configs for AmneziaWG 1.5
├── shadowsocks.sh          # Shadowsocks install + management
├── openvpn.sh              # OpenVPN install + management
├── xray.sh                 # XRay VLESS+REALITY install + management
├── xhttp.sh                # XRay XHTTP (CDN) install + management
├── xhttp-nginx.sh          # XRay XHTTP+Nginx (decoy website) install + management
└── docs/
    ├── CLIENT_SETUP.md     # This file
    ├── XHTTP_SETUP.md      # XHTTP deployment guide
    └── TROUBLESHOOTING.md
```

## Architecture Overview

### Direct VPN Connection

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CLIENT                                                                     │
│  ┌─────────────────┐                                                        │
│  │   WireGuard     │─────────────────────────────────────────────────────▶ │
│  │   or OpenVPN    │              Direct connection                         │
│  │   or SS client  │              (Best performance)                        │
│  └─────────────────┘                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                           │
                                           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  SERVER                                                                     │
│  ┌─────────────────┐                                                        │
│  │   WireGuard     │───▶ Internet                                          │
│  │   port XXXXX    │                                                        │
│  └─────────────────┘                                                        │
│  ┌─────────────────┐                                                        │
│  │   OpenVPN       │───▶ Internet                                          │
│  │   port 1194     │                                                        │
│  └─────────────────┘                                                        │
│  ┌─────────────────┐                                                        │
│  │   Shadowsocks   │───▶ Internet                                          │
│  │   port YYYYY    │                                                        │
│  └─────────────────┘                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### XRay VLESS+REALITY (For Censored Networks - Recommended)

Best obfuscation method as of late 2025. Traffic appears as legitimate HTTPS.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CLIENT                                                                      │
│  ┌─────────────────┐                                                        │
│  │   AmneziaVPN    │   TLS-like traffic to browser.yandex.com              │
│  │                 │──────────────────────────────────────────────────────▶│
│  │ VLESS+REALITY   │   Indistinguishable from normal HTTPS                 │
│  └─────────────────┘                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                           │
                                           ▼ Looks like HTTPS to DPI
┌─────────────────────────────────────────────────────────────────────────────┐
│  SERVER                                                                      │
│  ┌─────────────────┐                                                        │
│  │   XRay          │───▶ Internet                                          │
│  │   port 443      │                                                        │
│  │   VLESS+REALITY │                                                        │
│  └─────────────────┘                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## XRay VLESS+REALITY Setup (Recommended for Censorship Bypass)

### Why VLESS+REALITY?

- **No domain required** - unlike other TLS-based protocols
- **No certificate** - uses REALITY to mimic real TLS handshake
- **Undetectable** - traffic looks identical to normal HTTPS
- **Fast** - minimal overhead compared to older obfuscation methods

### Server-Side: Create User

```bash
# Using main menu
sudo ./vpn-manager.sh
# Select: 4) XRay → 1) Add client

# Or directly
sudo ./xray.sh
# Select: 1) Add client
```

After creating a user, you'll see:
- VLESS URL (for copy/paste) - **Use this to connect**
- QR Code (for future compatibility - not currently supported by AmneziaVPN)
- Configs saved to `/etc/vpn/xray/clients/`

### Client: Both for XRAY AND AmneziaWG  (Recommended)

| Platform | Download |
|----------|----------|
| Windows | [AmneziaVPN Windows](https://github.com/amnezia-vpn/amnezia-client/releases) |
| Windows | [AmneziaWG 1.5 for Windows ***](https://github.com/vayulqq/amneziawg-windows-client) |
| macOS | [AmneziaVPN macOS](https://github.com/amnezia-vpn/amnezia-client/releases) |
| Linux | [AmneziaVPN Linux](https://github.com/amnezia-vpn/amnezia-client/releases) |
| Android | [Play Store](https://play.google.com/store/apps/details?id=org.amnezia.vpn) or [GitHub](https://github.com/amnezia-vpn/amnezia-client/releases) |
| iOS | [App Store](https://apps.apple.com/app/amneziavpn/id1600529900) |

*** - WARNING! Random unsigned app. The only 3rd party Windows GUI client I found for specifically AmneziaWG 1.5, official AmneziaVPN also supports it, but it's difficult and doesn't support export, etc... There are rumors multiple parties are working on 3rd party clients for that including android. Server part is a normal Wireguard - there are no changes on server part, all obfuscation is made on client. Latest official AmneziaVPN for Windows also supports AmneziaWG 1.5.

### Connecting with AmneziaVPN

> **Important:** As of December 2025, AmneziaVPN requires using the **VLESS URL** method.
> QR code scanning is not yet supported for VLESS+REALITY but may work in future versions.

**VLESS URL Method (Windows/macOS/Linux/Android/iOS)**

1. Copy the VLESS URL from server output:
   ```
   vless://uuid@server:443?encryption=none&security=reality&sni=browser.yandex.com&fp=chrome&pbk=publickey&sid=shortid&flow=xtls-rprx-vision#AmneziaVPN-username
   ```
2. Open AmneziaVPN
3. Click "+" or "Add connection"
4. Select "Add config from clipboard" or paste URL manually
5. Connect

**QR Code (Not Currently Supported)**

QR codes are generated for future compatibility. When AmneziaVPN adds support:
1. On server, show client config: `sudo ./xray.sh` → Show client config & QR
2. Open AmneziaVPN on phone
3. Tap "+" → "Scan QR code"
4. Scan the terminal QR code

### Alternative Clients

Other clients that support VLESS+REALITY:

| Platform | Client |
|----------|--------|
| Windows | [Throne](https://github.com/nicejudy/nicejudy-throne-gui/releases) - see [setup guide](https://github.com/MarvinFS/Public/blob/main/throne/README.md), [v2rayN](https://github.com/2dust/v2rayN/releases) |
| macOS | [V2rayU](https://github.com/yanue/V2rayU/releases) |
| Android | [v2rayNG](https://github.com/2dust/v2rayNG/releases) |
| iOS | Shadowrocket (paid), Streisand |

---

## XRay CDN Tunnel Setup (For Heavily Censored Networks)

### Why CDN Tunnel?

When direct VLESS+REALITY gets blocked (e.g. Russia's TSPU fingerprinting REALITY handshakes), CDN tunneling routes traffic through Cloudflare. The censor sees a normal HTTPS connection to Cloudflare - indistinguishable from browsing any website.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CLIENT                                                                      │
│  ┌─────────────────┐                                                        │
│  │   v2rayNG /     │   HTTPS to Cloudflare (normal website traffic)        │
│  │   NekoBox       │──────────────────────────────────────────────────────▶│
│  │ VLESS+gRPC      │   Russia sees: connection to Cloudflare CDN IP       │
│  └─────────────────┘                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼ Cloudflare CDN (encrypted tunnel)
┌─────────────────────────────────────────────────────────────────────────────┐
│  SERVER                                                                      │
│  ┌─────────────────┐   ┌─────────────────┐                                │
│  │   cloudflared   │──▶│   XRay gRPC     │───▶ Internet                  │
│  │   (CF tunnel)   │   │   port 10443    │                                │
│  └─────────────────┘   └─────────────────┘                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Server-Side: Create User

```bash
# Using main menu
sudo ./vpn-manager.sh
# Select: 5) XRay CDN Tunnel → 1) Add client

# Or directly
sudo ./xhttp.sh
# Select: 1) Add client
```

After creating a user, you'll see:
- VLESS URL (for copy/paste) - **Use this to connect**
- QR Code (for mobile scanning)
- Configs saved to `/etc/vpn/xhttp/clients/`

### Client Apps

> **AmneziaVPN does NOT work** with the CDN Tunnel (gRPC) setup as of March 2026. It parses the VLESS URL but fails to route traffic. Use one of the clients below instead. AmneziaVPN works fine with the direct VLESS+REALITY setup (section above).

| Platform | Client | Notes |
|----------|--------|-------|
| Android | [v2rayNG](https://github.com/2dust/v2rayNG/releases) | Best gRPC support, recommended |
| Android | [NekoBox](https://github.com/MatsuriDayo/NekoBoxForAndroid/releases) | Alternative, sing-box based |
| Multi-platform | [Hiddify](https://github.com/hiddify/hiddify-app/releases) | Windows/Android/iOS |
| Windows | [v2rayN](https://github.com/2dust/v2rayN/releases) | Desktop client |

> **Note on AmneziaWG 1.5:** v2rayNG and NekoBox do NOT support AmneziaWG 1.5 obfuscated WireGuard. If you need both CDN Tunnel and AmneziaWG 1.5, you'll need two apps: v2rayNG/NekoBox for CDN Tunnel connections, and AmneziaVPN for AmneziaWG 1.5 connections. AmneziaVPN remains the only client supporting AmneziaWG 1.5.

### Connecting

1. Copy the VLESS URL from server output:
   ```
   vless://uuid@logs.example.com:443?encryption=none&security=tls&sni=logs.example.com&type=grpc&serviceName=xh&mode=multi&fp=chrome#logs.example.com-username
   ```
2. Open v2rayNG (or other client)
3. Tap "+" > "Import config from clipboard"
4. Connect

### Full Deployment Guide

See [XHTTP_SETUP.md](XHTTP_SETUP.md) for complete server deployment including Cloudflare setup.

---

## XRay XHTTP+Nginx Setup (Strongest Anti-Censorship)

### Why XHTTP+Nginx?

When both VLESS+REALITY and CDN tunneling fail (e.g. Russia's TSPU blocking Cloudflare tunnel traffic), the XHTTP+Nginx approach provides the strongest camouflage. Nginx serves a real website (with a valid Let's Encrypt certificate) to anyone who visits the domain, while VPN traffic is hidden on a specific path using XHTTP stream-up transport. DPI probes see a legitimate HTTPS website with real content.

```
CLIENT                                     SERVER (port 443)
  v2rayNG          HTTPS to domain    ┌───────────────────────┐
  VLESS+XHTTP  ─────────────────────▶│  Nginx (TLS)          │
  stream-up        DPI sees: normal   │    /  → decoy website │
                   website traffic    │    /path/ → xray      │
                                      │  ┌─────────────────┐  │
                                      │  │ XRay XHTTP      │  │
                                      │  │ (Unix socket)   │──▶ Internet
                                      │  └─────────────────┘  │
                                      └───────────────────────┘
```

### Server-Side: Create User

```bash
# Using main menu
sudo ./vpn-manager.sh
# Select: 6) XRay XHTTP+Nginx → 1) Add client

# Or directly
sudo ./xhttp-nginx.sh
# Select: 1) Add client
```

After creating a user, you'll see:
- VLESS URL (for copy/paste)
- QR Code (for mobile scanning)
- Configs saved to `/etc/vpn/xhttp-nginx/clients/`

### Client Apps

Same clients as the CDN Tunnel setup. AmneziaVPN compatibility is TBD.

| Platform | Client | Notes |
|----------|--------|-------|
| Android | [v2rayNG](https://github.com/2dust/v2rayNG/releases) | Best XHTTP support, recommended |
| Android | [NekoBox](https://github.com/MatsuriDayo/NekoBoxForAndroid/releases) | Alternative, sing-box based |
| Multi-platform | [Hiddify](https://github.com/hiddify/hiddify-app/releases) | Windows/Android/iOS |
| Windows | [v2rayN](https://github.com/2dust/v2rayN/releases) | Desktop client |

### Connecting

1. Copy the VLESS URL from server output:
   ```
   vless://uuid@logs.example.com:443?encryption=mlkem768x25519plus.native.0rtt.KEY&security=tls&sni=logs.example.com&type=xhttp&path=/ingest/v2/&mode=stream-up&fp=chrome&flow=xtls-rprx-vision#logs.example.com-username
   ```
2. Open v2rayNG (or other client)
3. Tap "+" > "Import config from clipboard"
4. Connect

Note: The encryption parameter contains the VLESS Encryption (vlessenc) key, and flow enables XTLS Vision optimization. Both are generated automatically during server setup. Client apps must support vlessenc (v2rayNG 1.9+, Hiddify, v2rayN with xray-core 26.3+).

### Key Differences from CDN Tunnel

- Traffic goes directly to your server (no Cloudflare middleman)
- Real Let's Encrypt certificate (not self-signed behind CF)
- Decoy website responds to casual visitors and DPI probes
- Uses XHTTP transport (not gRPC) - the modern replacement
- VLESS Encryption provides protocol-level encryption with post-quantum key exchange
- Requires a domain with DNS pointing to your server (A record, not proxied)

---

## WireGuard Setup

### Server-Side: Create User

```bash
# Using main menu
sudo ./vpn-manager.sh
# Select: 1) WireGuard → Add client

# Or directly
sudo ./wireguard.sh
# Select: Add client
```

During user creation, you can choose:
- **Standard WireGuard** - works with any WireGuard client
- **AmneziaWG 1.5** - adds obfuscation parameters (requires AmneziaWG 1.5 client or AmneziaVPN client)

### Client Downloads - look above, same client as for the XRAY 

> ⚠️ **Important**: AmneziaWG 1.5 configs with `Jc`, `Jmin`, `Jmax`, `I1` parameters **only work with AmneziaWG 1.5 client or official AmneziaVPN for Windows**. Standard WireGuard client will crash.

### Client Configuration

**Option A: QR Code (Mobile)**
1. Open WireGuard/AmneziaWG app
2. Tap "+" → "Scan from QR code"
3. Scan the QR code shown during user creation

**Option B: Config File (Desktop)**
1. Copy the `.conf` file from `/etc/vpn/wireguard/clients/`
2. Import into WireGuard/AmneziaWG app
3. Activate tunnel

---

## OpenVPN Setup

### Server-Side: Create User

```bash
sudo ./vpn-manager.sh
# Select: 2) OpenVPN → Add client

# Or directly
sudo ./openvpn.sh
```

### Client Downloads

| Platform | Download |
|----------|----------|
| Windows | [OpenVPN GUI](https://openvpn.net/community-downloads/) |
| macOS | [Tunnelblick](https://tunnelblick.net/) or OpenVPN Connect |
| Linux | `apt install openvpn` or `dnf install openvpn` |
| Android | [OpenVPN Connect](https://play.google.com/store/apps/details?id=net.openvpn.openvpn) |
| iOS | [OpenVPN Connect](https://apps.apple.com/app/openvpn-connect/id590379981) |

### Client Configuration

1. Copy `.ovpn` file from `/etc/vpn/openvpn/clients/`
2. Import into OpenVPN client
3. Connect

---

## Shadowsocks Setup

### Server-Side: Get Config

```bash
sudo ./vpn-manager.sh
# Select: 3) Shadowsocks → Show config & QR

# Or directly
sudo ./shadowsocks.sh
```

### Client Downloads

| Platform | Download |
|----------|----------|
| Windows | [Shadowsocks-windows](https://github.com/shadowsocks/shadowsocks-windows/releases) |
| macOS | [ShadowsocksX-NG](https://github.com/shadowsocks/ShadowsocksX-NG/releases) |
| Linux | `apt install shadowsocks-libev` |
| Android | [Shadowsocks](https://play.google.com/store/apps/details?id=com.github.shadowsocks) |
| iOS | Shadowrocket (paid) or Potatso Lite |

### Client Configuration

Use the connection details from the server:
- Server: `YOUR_SERVER_IP`
- Port: `8388` (default)
- Password: (shown in config)
- Encryption: `chacha20-ietf-poly1305`

Or scan the QR code / use the SS URL.

---

## Verify Connection

```bash
# Check your public IP (should show server's IP)
curl ifconfig.me
```

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common issues and solutions.
