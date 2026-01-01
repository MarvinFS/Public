# Xray UDP Transparent Proxy - Advanced Port-Based Routing

Advanced configuration guide for enabling UDP transparent proxy with port-based routing for messaging app voice/video calls on OpenWrt.

> **Prerequisites**: Complete the basic setup in [README_runetfreedom_geodata_updater.md](README_runetfreedom_geodata_updater.md) first.

## Target System

- **Router**: GL.iNet GL-MT6000 (Flint 2)
- **Platform**: mediatek/filogic (ARMv8 64-bit ARM)
- **OpenWrt**: 24.10.5 (kernel 6.6.119)
- **Xray**: Installed via openwrt-xray + luci-app-xray

## Why UDP Transparent Proxy?

By default, the recommended setup uses **TCP-only** transparent proxy because enabling UDP previously caused DNS breakage. However, with proper port exceptions, UDP proxy can work safely and enable:

- Telegram voice/video calls
- WhatsApp voice/video calls  
- FaceTime calls
- Discord voice channels
- Other VoIP applications (if you know exact port range and make changes manually)

## How Custom Configuration Hook Works

The `custom_configuration_hook` in luci-app-xray uses **ucode** (not JavaScript, despite similar syntax). It:

1. **Receives** the config object built from LuCI GUI settings
2. **Modifies** specific sections (adds/changes values)
3. **Returns** the modified config

**Key insight**: Custom hook **ADDS TO** or **MODIFIES** the LuCI-generated config - it doesn't replace it entirely. So LuCI's "Bypassed UDP Ports" and custom hook routing rules work together.

```
LuCI GUI Settings → Generated Base Config → Custom Hook Modifies → Final Xray Config
```

---

## Phase 1: Enable UDP Transparent Proxy with DNS Protection

### Step 1.1: Verify kernel modules

```bash
# Check TPROXY modules are loaded
lsmod | grep tproxy
# Expected output:
# nft_tproxy   ...
# nf_tproxy_ipv4   ...
# nf_tproxy_ipv6   ...

# If missing, install them
opkg update
opkg install kmod-nft-tproxy kmod-nf-tproxy
```

### Step 1.2: Configure UDP bypass ports in LuCI

Go to **Services** → **Xray** → **Outbound Routing** tab:

1. **Bypassed UDP Ports**: Add these ports (one per line):
   - `53` (DNS - CRITICAL)
   - `443` (QUIC - prevents loops)

2. Keep existing settings:
   - **GeoIP Direct Code List (IPv4)**: `RU`
   - **GeoIP Direct Code List (IPv6)**: `RU`
   - **Bypassed IP**: Keep your existing entries if any

3. **Save** (don't apply yet)

### Step 1.3: Enable UDP Server in LuCI

Go to **Services** → **Xray** → **General Settings** tab:

1. Find **UDP Server (IPv4)**
2. Select your server profile (e.g., `remoteXray`)
3. **Save & Apply**

### Checkpoint 1: Verify DNS Still Works

**⚠️ STOP HERE IF DNS BREAKS - See Rollback section below**

```bash
# Test DNS resolution from router
nslookup google.com
# Expected: Returns IP address

# Test DNS from LAN client
nslookup google.com 192.168.1.1
# Expected: Returns IP address

# Check Xray is running
pidof xray && echo "Xray running" || echo "ERROR: Xray not running!"

# Check for DNS-related errors
logread | grep -i 'dns\|xray' | tail -20
```

**If DNS works → Proceed to Phase 2**
**If DNS fails → Go to Rollback section immediately**


> **⚠️ IMPORTANT: Phase 2 is OPTIONAL**
> 
> After completing Phase 1, you already have **full UDP transparent proxy** working! All UDP traffic (except bypassed ports 53, 443, 5353, 123, 111) now routes through your proxy automatically.
> 
> **Telegram, WhatsApp, Discord, and other VoIP apps should already work at this point.**

```bash
# Save in LuCI, then validate from CLI
/usr/bin/xray run -test -confdir /var/etc/xray | head -50
# Expected: Configuration OK.

# If validation fails, check the error and fix custom hook syntax
```

### Checkpoint 2: Verify Generated Config

Verify that BOTH LuCI bypass ports AND custom hook rules appear in the generated config:

```bash
# Check UDP bypass ports from LuCI are present
nft list ruleset | grep -E "53|443|123"

# Full routing rules inspection (shows rule order)
cat /var/etc/xray/config.json | grep -A 100 '"routing"' | head -120
```

**Expected**: You should see routing rules for ports 599, 1400, 16384-16402, 3478-3497, 50000-65535 with `outboundTag: "proxy"`.

---

## Phase 3: Test UDP Routing

### Step 3.1: General UDP Verification with tcpdump

```bash
# Install tcpdump if not present
opkg update
opkg install tcpdump

# Monitor UDP traffic on WAN interface (adjust interface name if needed)
# In one terminal:
tcpdump -i eth1 udp and not port 53 -n

# From a LAN client, make a Telegram or WhatsApp call
# You should NOT see the call traffic on WAN (it goes through proxy)
```

### Step 3.2: Test Telegram Calls

1. Open Telegram on your phone (connected to router WiFi)
2. Make a voice call to any contact
3. Call should connect and work clearly

**Verify in logs:**
```bash
# Check Xray routing decisions (enable debug temporarily)
uci set xray_core.@general[0].loglevel='debug'
uci commit xray_core
/etc/init.d/xray_core restart

# Make a Telegram call, then check logs
logread | grep -E 'udp.*599|udp.*1400' | tail -20

# Restore warning level when done
uci set xray_core.@general[0].loglevel='warning'
uci commit xray_core
/etc/init.d/xray_core restart
```

### Step 3.3: Test WhatsApp Calls

1. Open WhatsApp on your phone
2. Make a voice or video call
3. Call should connect and work clearly

**Monitor specific WhatsApp ports:**
```bash
# Watch for STUN/TURN traffic (ports 3478-3497)
tcpdump -i br-lan udp port 3478 or udp port 3479 -n -c 20
```

### Checkpoint 3: Verify Calls Work

**If all tests pass → UDP transparent proxy is working!**

---

## Rollback Procedure

If DNS breaks or connectivity issues occur after enabling UDP:

### Immediate Rollback (restore TCP-only)

```bash
# Stop Xray
/etc/init.d/xray_core stop

# Hard kill if needed
killall -q xray 2>/dev/null || true

# Clean generated config
rm -rf /var/etc/xray/*

# Restart base services
/etc/init.d/dnsmasq restart
/etc/init.d/firewall restart
```

### Disable UDP in LuCI

1. Go to **Services** → **Xray** → **General Settings**
2. Set **UDP Server (IPv4)** to `your upstream server name` (disabled)
3. **Save & Apply**

### Restart Xray

```bash
/etc/init.d/xray_core start

# Verify
/etc/init.d/xray_core status
pidof xray && echo "Xray running" || echo "ERROR"

# Test DNS
nslookup google.com
```

---

## Troubleshooting

### DNS breaks after enabling UDP

**Cause**: UDP port 53 not bypassed properly.

**Fix**:
1. Ensure `53` is in **Bypassed UDP Ports** in LuCI Outbound Routing tab
2. Check generated config: `nft list ruleset | grep -E "53|443|5353|123|111"`

### Telegram calls don't connect

**Cause**: UDP ports 599/1400 not routed through proxy.

**Debug**:
```bash
# Check if rules exist in config
cat /var/etc/xray/config.json | grep -A 3 '"599"'

# Monitor traffic
tcpdump -i br-lan udp port 599 or udp port 1400 -n
```

### WhatsApp calls fail

**Cause**: STUN/TURN ports (3478-3497) not routing correctly.

**Debug**:
```bash
# Check if rules exist
cat /var/etc/xray/config.json | grep -A 3 '"3478"'

# Check if STUN is reaching the proxy
logread | grep -i stun
```

### Config validation fails
```bash
# Show exact error
/usr/bin/xray run -test -confdir /var/etc/xray 2>&1 | head -100
```

### Some UDP traffic still bypasses proxy
**Cause**: The port isn't covered by routing rules.
**Fix**: Add the specific port to custom hook routing rules.

---

## Port Reference
### Messaging Apps UDP Ports
| Application | UDP Ports | Purpose |
|-------------|-----------|---------|
| Telegram | 599, 1400 | Voice/video calls |
| WhatsApp | 3478-3497, 5242-5243, 4244, 7985 | STUN/TURN, media |
| Viber | 5242-5243, 7985, 4244 | Voice/video |
| FaceTime | 16384-16387, 16393-16402 | Audio/video RTP |
| Discord | 50000-65535 | Voice channels |

### Bypassed UDP Ports (Direct, no proxy)

| Port | Purpose | Why Bypass |
|------|---------|------------|
| 53 | DNS | Prevents DNS loops |
| 443 | QUIC/HTTP3 | Prevents routing loops |
| 123 | NTP | Network Time Protorol |

---

## References

- [Xray-core Routing Documentation](https://xtls.github.io/config/routing.html)
- [Xray-core DNS Documentation](https://xtls.github.io/config/dns.html)
- [openwrt-xray GitHub](https://github.com/yichya/openwrt-xray)
- [luci-app-xray GitHub](https://github.com/yichya/luci-app-xray)
- [RuNetFreedom Geodata Updater](README_runetfreedom_geodata_updater.md)

## See Also

- [open_wrt_xray_vless_reality_full_how_to_and_troubleshooting_summary.md](open_wrt_xray_vless_reality_full_how_to_and_troubleshooting_summary.md) - Main OpenWrt Xray setup guide
- [README_runetfreedom_geodata_updater.md](README_runetfreedom_geodata_updater.md) - RuNetFreedom geodata setup (prerequisite)
