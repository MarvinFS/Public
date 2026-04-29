# XRay CDN Tunnel (gRPC) - Deployment Guide

Last updated: 2026-03-27

> **Part of Linux VPN Manager** - See [README.md](../README.md) for full documentation.
> For client connection instructions, see [CLIENT_SETUP.md](CLIENT_SETUP.md).

## Overview

Routes VPN traffic through Cloudflare CDN using VLESS+gRPC transport via Cloudflare Tunnel. Censors see a TLS connection to Cloudflare's IP - identical to normal website browsing. No port forwarding needed.

```
Phone (AmneziaVPN / v2rayNG)
   |  HTTPS, port 443
   v
Cloudflare CDN Edge   <-- Censor sees only this (Cloudflare IP + your domain)
   |  Tunnel (--protocol http2)
   v
cloudflared           <-- Outbound tunnel, no inbound ports needed
   |  HTTPS + HTTP/2 (gRPC)
   v
Xray gRPC+TLS         <-- Self-signed cert, 127.0.0.1:10443
   |
   v
Internet
```

### Requirements

- Linux server (Ubuntu 22.04+, Debian 10+, AlmaLinux/Rocky 9+)
- Domain on Cloudflare (free plan)
- Cloudflare account (free) with Zero Trust enabled (free tier, $0)

### What Gets Installed

- `xray-xhttp.service` - Xray with gRPC transport + self-signed TLS
- `cloudflared.service` - Cloudflare Tunnel daemon with `--protocol http2`
- Config: `/usr/local/etc/xray-xhttp/`
- Clients: `/etc/vpn/xhttp/clients/`

---

## Step 1: Cloudflare Account & Domain

1. Sign up at cloudflare.com (free plan)
2. Click "Add a site", enter your domain
3. Review auto-imported DNS records
4. Change nameservers at your registrar to the two Cloudflare NS addresses
5. Wait for propagation (minutes to hours)

## Step 2: Cloudflare Dashboard Settings

Once domain shows "Active":

Enable gRPC (required): domain > Network > toggle "gRPC" ON

Set SSL mode: domain > SSL/TLS > "Full"

Disable Browser Integrity Check: domain > Security > Settings > toggle OFF

## Step 3: Create Cloudflare Tunnel

1. CF dashboard > Zero Trust > Networking > Tunnels
2. Create tunnel, select "Cloudflared" connector
3. Name your tunnel
4. Copy the tunnel install token (long `eyJ...` string)
5. Do NOT add a public hostname route in the dashboard - we configure it via API in Step 5

## Step 4: Run Installer on Server

```bash
sudo ./vpn-manager.sh    # Select: 5) XRay CDN Tunnel
# Or directly:
sudo ./xhttp.sh          # Select: 1) Install
```

The installer prompts for your domain and tunnel token, then sets up everything automatically including the self-signed TLS certificate and `--protocol http2` (critical for gRPC stability).

## Step 5: Configure Tunnel Origin Settings (Critical)

The tunnel route needs "HTTP2 connection" and "No TLS Verify" enabled. These settings are in the **Zero Trust dashboard** (not the main CF dashboard).

### Prerequisite: Enable Zero Trust

If you haven't already: CF dashboard > Zero Trust > Get started > Free Plan ($0). Requires address and payment method on file but won't charge.

### Configure via Zero Trust Dashboard

1. Go to Zero Trust dashboard (one.dash.cloudflare.com)
2. **Networks** > **Connectors** > click your tunnel
3. Select **Published application routes** tab at the top
4. Click the **three dots** menu on your route > **Edit**
5. Set Service URL to `https://localhost:10443`
6. Expand **Additional application settings**
7. Under **TLS**: set **No TLS Verify** to **ON**
8. Under **TLS**: set **HTTP2 connection** to **ON**
9. Save

These settings are NOT available in the simplified "Networking > Tunnels" UI on the main CF dashboard. You must use the Zero Trust dashboard.

### Alternative: via API

Same settings can be applied via Cloudflare API using an Account API token with Cloudflare Tunnel:Edit + Zero Trust:Edit permissions. See [CF tunnel configurations API](https://developers.cloudflare.com/api/) - PUT endpoint `accounts/{id}/cfd_tunnel/{id}/configurations` with `originRequest.http2Origin` and `originRequest.noTLSVerify` in the ingress config.

### Create DNS Record

If the route creation didn't auto-create it, add manually in CF dashboard > DNS:

- Type: CNAME
- Name: your subdomain (e.g. `logs`)
- Target: `TUNNEL_UUID.cfargotunnel.com`
- Proxy status: Proxied (orange cloud)

## Step 6: Create Clients

```bash
sudo ./xhttp.sh    # Select: 1) Add client
```

Generates a VLESS URL and QR code. Import into AmneziaVPN, v2rayNG, or Hiddify.

---

## Why gRPC (Not XHTTP or WebSocket)

We tested all transports through Cloudflare Tunnel:

XHTTP (splithttp/stream-up): cloudflared kills persistent HTTP streams. Every XHTTP session terminates immediately with "stream canceled". Fundamentally incompatible with cloudflared's HTTP/1.1 forwarding.

WebSocket: Works through cloudflared but AmneziaVPN's WS client implementation is broken (shows "connected" but sends zero traffic to server). Works with v2rayNG/v2RayTun.

gRPC: Works through cloudflared when three conditions are met: (1) origin uses HTTPS with self-signed TLS cert including SAN, (2) tunnel route has `http2Origin: true` + `noTLSVerify: true` set via CF API, (3) cloudflared runs with `--protocol http2` (not default QUIC). AmneziaVPN natively parses `type=grpc` in VLESS URLs.

## Key Settings Explained

`--protocol http2` on cloudflared: The default QUIC protocol causes "context canceled" errors every few seconds with gRPC. Switching to HTTP/2 eliminates these completely.

`http2Origin: true` in tunnel route: Makes cloudflared connect to origin via HTTP/2 (required for gRPC). Only configurable via CF API, not dashboard UI.

`noTLSVerify: true` in tunnel route: Accepts self-signed TLS cert on origin. Required because we use a self-signed cert for HTTP/2 ALPN negotiation.

`multiMode: true` in gRPC settings: Multiplexes connections over fewer gRPC streams, reducing overhead.

`initial_windows_size: 65536` in gRPC settings: Disables dynamic window to prevent Cloudflare from sending unexpected h2 GOAWAY frames.

---

## Troubleshooting

cloudflared "context canceled" spam: Add `--protocol http2` to the ExecStart in `/etc/systemd/system/cloudflared.service`, then `systemctl daemon-reload && systemctl restart cloudflared`.

cloudflared "certificate signed by unknown authority": The self-signed cert needs SAN (Subject Alternative Name). Regenerate: `openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 3650 -nodes -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"`

cloudflared "legacy Common Name field": Same fix as above - cert needs `-addext "subjectAltName=..."`.

HTTP 502 through tunnel: Check `journalctl -u cloudflared -n 20` for the specific error. Usually a TLS or HTTP/2 issue.

No traffic reaches server: Verify DNS CNAME exists for your subdomain pointing to `TUNNEL_UUID.cfargotunnel.com` with Proxied (orange cloud) enabled.

AmneziaVPN "connected" but no internet: AmneziaVPN's WebSocket implementation is broken. Use `type=grpc` in the VLESS URL (natively supported by AmneziaVPN).
