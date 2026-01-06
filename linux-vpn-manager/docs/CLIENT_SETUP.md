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
└── docs/
    ├── CLIENT_SETUP.md     # This file
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
| Windows | [v2rayN](https://github.com/2dust/v2rayN/releases), [Nekoray](https://github.com/MatsuriDayo/nekoray/releases) |
| macOS | [V2rayU](https://github.com/yanue/V2rayU/releases) |
| Android | [v2rayNG](https://github.com/2dust/v2rayNG/releases) |
| iOS | Shadowrocket (paid), Streisand |

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
