# Troubleshooting Guide

Last updated: 2026-01

> **Part of Linux VPN Manager** - See [README.md](../README.md) for full documentation.

## Project Structure

```
linux-vpn-manager/
├── vpn-manager.sh          # Main entry point
├── common.sh               # Shared library
├── wireguard.sh            # WireGuard install + management
├── shadowsocks.sh          # Shadowsocks install + management
├── openvpn.sh              # OpenVPN install + management
├── xray.sh                 # XRay VLESS+REALITY install + management
└── docs/
    ├── CLIENT_SETUP.md
    └── TROUBLESHOOTING.md  # This file
```

## Quick Diagnostics

### Unified Status Check

```bash
# Check all services at once
sudo ./vpn-manager.sh
# Main menu shows status of all VPN services

# Or check individual VPNs
sudo ./wireguard.sh      # Shows WireGuard status
sudo ./shadowsocks.sh    # Shows Shadowsocks status
sudo ./openvpn.sh        # Shows OpenVPN status
sudo ./xray.sh           # Shows XRay status
```

### Individual Service Status

```bash
# WireGuard
systemctl status wg-quick@wg0
wg show

# OpenVPN
systemctl status openvpn-server@server

# Shadowsocks
systemctl status shadowsocks

# XRay
systemctl status xray
```

### Check Listening Ports

```bash
# All VPN ports
ss -tulnp | grep -E '443|1194|8388|51820'

Here the example only you need to specify real used ports
# Expected (default ports):
# 443   - XRay VLESS+REALITY
# 1194  - OpenVPN
# 8388  - Shadowsocks
# 51820 - WireGuard (UDP)
```

### View Logs

```bash
# Real-time logs
journalctl -u wg-quick@wg0 -f
journalctl -u openvpn-server@server -f
journalctl -u shadowsocks -f
journalctl -u xray -f

# Last 50 lines
journalctl -u SERVICE_NAME -n 50
```

---

## Common Problems
# Check your public IP (should show server's IP)
```bash
curl ifconfig.me
```

### IP Forwarding Issues (Most Common!)

If client connects but has no internet access:

```bash
# Check if IP forwarding is enabled
sysctl net.ipv4.ip_forward
sysctl net.ipv6.conf.all.forwarding

# Should show: = 1
# If = 0, enable it:
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv6.conf.all.forwarding=1

# Make permanent
echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf
echo "net.ipv6.conf.all.forwarding = 1" >> /etc/sysctl.conf
sysctl -p
```

### NAT/iptables Issues

```bash
# Check NAT rules exist
iptables -t nat -L POSTROUTING -n -v | grep MASQUERADE

# If missing, add manually:
iptables -t nat -A POSTROUTING -o ens18 -j MASQUERADE  # Replace ens18 with your interface

# Check FORWARD chain
iptables -L FORWARD -n -v

# Save rules
netfilter-persistent save
```

### Connection Issues

| Problem | Cause | Solution |
|---------|-------|----------|
| Connection refused | Service not running | `systemctl start SERVICE_NAME` |
| Connection timeout | Firewall blocking | Open required ports (see Firewall section) |
| Handshake failed | Wrong credentials | Regenerate user config |
| DNS not working | DNS leak/misconfiguration | Check DNS settings in client config |
| **Connected but no internet** | **IP forwarding disabled** | **See IP Forwarding section above** |

---

## XRay VLESS+REALITY Specific

| Problem | Cause | Solution |
|---------|-------|----------|
| Connection timeout | Port 443 blocked | Check firewall and cloud provider rules |
| VLESS URL not working | Incorrect format | Regenerate client config, ensure no URL encoding issues |
| QR code won't scan | Terminal encoding | Try copy/paste VLESS URL instead |
| Client connects but no traffic | Wrong shortId | Verify shortId matches between client and server |
| Reality handshake failed | SNI blocked | Try different SNI target site |
| UUID rejected | Client revoked | Check if client exists in server config |

### Verify XRay Configuration

```bash
# Check XRay service
systemctl status xray

# Check XRay config syntax
xray -test -config /etc/xray/config.json

# View active clients
ls /etc/vpn/xray/clients/

# Check XRay listening
ss -tlnp | grep xray
```

### XRay Client Debug

In AmneziaVPN:
1. Enable logging in settings
2. Check connection log for specific errors
3. Verify all parameters match:
   - UUID
   - Public Key (pbk)
   - Short ID (sid)
   - SNI (sni)
   - Fingerprint (fp)

### Common XRay Errors

**"context deadline exceeded"**
- Server unreachable or port blocked
- Check: `telnet SERVER_IP 443`

**"failed to dial to server"**
- Wrong server address or port
- Firewall blocking connection

**"unknown short ID"**
- Client's shortId not in server's shortIds array
- Regenerate client config or rebuild server config

---

## WireGuard Specific

| Problem | Cause | Solution |
|---------|-------|----------|
| Handshake timeout | Wrong endpoint | Verify `Endpoint` in client config |
| Handshake timeout | Firewall blocking UDP | Open port (check `/etc/wireguard/params` for port) |
| No internet after connect | IP forwarding disabled | Enable IP forwarding (see above) |
| No internet after connect | NAT rules missing | Check iptables MASQUERADE rules |
| MTU issues | Packet fragmentation | Lower MTU to 1280-1420 |
| AmneziaWG QR fails | Config too large | Use file import instead of QR |

---

## OpenVPN Specific

| Problem | Cause | Solution |
|---------|-------|----------|
| TLS handshake failed | Certificate mismatch | Regenerate client config |
| AUTH_FAILED | Wrong credentials | Check username/password |
| TAP adapter missing | Driver not installed | Reinstall OpenVPN/TAP driver |
| Cannot allocate TUN | TUN device busy | Restart OpenVPN or reboot |

---

## Shadowsocks Specific

| Problem | Cause | Solution |
|---------|-------|----------|
| Connection closed | Wrong password/cipher | Verify config matches server |
| Bad decrypt | Cipher mismatch | Use same cipher on both sides |
| Timeout | Port blocked | Try different port |

---

## Firewall Configuration

### Ubuntu/Debian (UFW)

```bash
# WireGuard
ufw allow 51820/udp

# OpenVPN
ufw allow 1194/udp

# Shadowsocks
ufw allow 8388/tcp

# XRay
ufw allow 443/tcp

# Verify
ufw status
```

### AlmaLinux/Rocky/CentOS (firewalld)

```bash
# WireGuard
firewall-cmd --permanent --add-port=51820/udp

# OpenVPN
firewall-cmd --permanent --add-port=1194/udp

# Shadowsocks
firewall-cmd --permanent --add-port=8388/tcp

# XRay
firewall-cmd --permanent --add-port=443/tcp

# Apply
firewall-cmd --reload
```

### iptables (Manual)

```bash
# WireGuard
iptables -A INPUT -p udp --dport 51820 -j ACCEPT

# OpenVPN
iptables -A INPUT -p udp --dport 1194 -j ACCEPT

# Shadowsocks
iptables -A INPUT -p tcp --dport 8388 -j ACCEPT

# XRay
iptables -A INPUT -p tcp --dport 443 -j ACCEPT

# Save
iptables-save > /etc/iptables/rules.v4
```

### Cloud Provider Firewalls

Don't forget to configure firewall rules in your cloud provider's console:

| Provider | Location |
|----------|----------|
| AWS | Security Groups → Inbound Rules |
| Azure | Network Security Groups |
| Google Cloud | VPC Firewall Rules |
| DigitalOcean | Networking → Firewalls |
| Vultr | Firewall |
| Linode | Firewalls |

---

## Verify Connection is Working

### From Client

```bash
# Check public IP (should show server's IP)
curl ifconfig.me
curl ip.me
curl ipinfo.io/ip

# Test specific connection
curl -v https://www.google.com
```

### WireGuard Handshake Check

```bash
# On client
wg show

# Look for:
# latest handshake: X seconds ago
# If > 2 minutes, connection may be dead
```

---

## Performance Issues

### Slow Speeds

1. **Enable BBR congestion control**
   ```bash
   sudo ./vpn-manager.sh
   # Select "Apply system optimizations"
   ```

2. **Check server load**
   ```bash
   htop
   iftop
   ```

3. **Test raw bandwidth**
   ```bash
   # Install iperf3 on both ends
   # Server: iperf3 -s
   # Client: iperf3 -c SERVER_IP
   ```

### High Latency

1. Choose server closer to your location
2. Try different VPN protocols (WireGuard is usually fastest)
3. Reduce MTU if experiencing fragmentation

---

## Reset / Reinstall

### Reset Single Component

```bash
# Use individual script
sudo ./xray.sh
# Select "Uninstall" then run install again
```

### Complete Reset

```bash
# Stop everything
systemctl stop wg-quick@wg0 openvpn-server@server shadowsocks xray

# Remove configs (CAUTION: deletes all user configs!)
rm -rf /etc/wireguard /etc/openvpn /etc/shadowsocks /etc/xray
rm -rf /etc/vpn  # Client configs
rm -rf /usr/local/etc/xray

# Reinstall using vpn-manager.sh
sudo ./vpn-manager.sh
```
---
### Debug Mode

```bash
# WireGuard verbose
wg show all dump

# Check IP forwarding and NAT
sysctl net.ipv4.ip_forward
iptables -t nat -L POSTROUTING -n -v
iptables -L FORWARD -n -v

# XRay config test
xray -test -config /etc/xray/config.json

# XRay verbose logging (edit config.json)
# Change "loglevel": "warning" to "loglevel": "debug"
systemctl restart xray
journalctl -u xray -f
```
