# RuNetFreedom Geodata Updater for OpenWrt

Automated updater for Russia-focused V2Ray geodata from [runetfreedom/russia-v2ray-rules-dat](https://github.com/runetfreedom/russia-v2ray-rules-dat).

## !!!WARNING!!!
**Russia-v2ray-rules are extremely memory heavy for example geosite:ru-blocked-all has 770k entries!!! I don't use this particular list, but with what's specified below my route alrady using almost 600MB of memory total!!! NOT all hardware routers are that capable**

## Target System specs:

- **Router**: GL.iNet GL-MT6000 (Flint 2)
- **Platform**: mediatek/filogic (ARMv8 64-bit ARM)
- **OpenWrt**: 24.10.5 (kernel 6.6.119)
- **Xray**: Installed via openwrt-xray + luci-app-xray

## Features

**Automatic downloads** from runetfreedom repository with SHA256 verification  
**Smart updates** - compares checksums first, skips download if already up-to-date  
**Symlink-based activation** - easy switching between runetfreedom and default geodata  
**Atomic replacement** with timestamped backups (one previous version retained)  
**Config validation** with automatic rollback on failure  
**Proper logging** to syslog with priorities (info/warn/error)  
**Service health checks** after restart  
**Dry-run mode** for testing without making changes  

## Understanding RuNetFreedom Geodata
### How it works

RuNetFreedom geodata is built **on top of** standard v2fly/Loyalsoldier geodata:

```
┌─────────────────────────────────────────────────────────────┐
│  RuNetFreedom geoip.dat / geosite.dat                       │
├─────────────────────────────────────────────────────────────┤
│  - ALL standard v2fly categories (geoip:ru, geoip:cn,       │
│    geosite:google, geosite:category-ru, etc.)               │
│  - PLUS Russia-specific categories (geosite:ru-blocked,     │
│    geosite:ru-available-only-inside, geoip:ru-blocked)      │
└─────────────────────────────────────────────────────────────┘
```

**This means:**
- `geoip:ru` (all Russian IP ranges) - **WORKS** - included from standard geodata
- `geosite:category-ru` (common Russian domains) - **WORKS** - included from v2fly
- `geosite:ru-blocked` (blocked in Russia) - **WORKS** - runetfreedom-specific
- `geosite:ru-available-only-inside` - **WORKS** - runetfreedom-specific

**Available Geosite Categories**
From runetfreedom repository:

- `geosite:ru-blocked` - Blocked domains in Russia (antifilter + re:filter) **← RECOMMENDED**
- `geosite:ru-blocked-all` - All known blocked domains (~700k domains, use with caution - how much memory your router has?)
- `geosite:ru-available-only-inside` - Domains only available inside Russia
- `geosite:antifilter-download` - All domains from antifilter.download (~700k)
- `geosite:antifilter-download-community` - Community antifilter list
- `geosite:refilter` - All domains from re:filter
- `geosite:category-ads-all` - All advertising domains
- `geosite:win-spy` - Windows telemetry and tracking
- `geosite:win-update` - Windows Update domains
- `geosite:win-extra` - Other Windows domains

Plus all categories from @v2fly/domain-list-community (google, discord, youtube, twitter, meta, openai, etc.)

### Auto-updating

The script downloads from the `release` **branch** (not specific tags), which is automatically updated **every 6 hours** with the latest blocked domains/IPs data.

**Smart update mechanism:**
1. Downloads checksum files first (small, ~100 bytes each)
2. Compares with currently installed files
3. If checksums match → skips download, exits immediately (saves bandwidth)
4. If checksums differ → downloads full geodata files and updates

This means you can run the script frequently (e.g., via cron every 6 hours) without wasting bandwidth on unnecessary downloads.

### File locations

```
Script:           /usr/local/sbin/runetfreedom-geodata-updater.sh
Geodatabase data: /usr/local/share/xray-assets/
  ├── geoip.dat   (downloaded from runetfreedom)
  ├── geosite.dat (downloaded from runetfreedom)
  └── backup/
      ├── geoip.dat.2025-12-27_130000
      └── geosite.dat.2025-12-27_130000

Active symlinks:  /usr/share/xray/
  ├── geoip.dat -> /usr/local/share/xray-assets/geoip.dat
  └── geosite.dat -> /usr/local/share/xray-assets/geosite.dat

Original data:    /usr/share/v2ray/  (fallback, untouched)
Cron schedule:    /etc/crontabs/root
Sysupgrade conf:  /etc/sysupgrade.conf
```

**Benefits:**
- Xray always reads from `/usr/share/xray/*.dat` (no configuration changes needed)
- Easy rollback: just change symlinks to point to `/usr/share/v2ray/` for original data
- Clean separation between runetfreedom data and system defaults

## Installation

### 1. Configure sysupgrade persistence (one-time setup)

Add paths to `/etc/sysupgrade.conf` to survive firmware upgrades:

```bash
# Add these lines to /etc/sysupgrade.conf MAKE SURE IT IS DONE ONLY ONCE!!!
echo "/usr/local/sbin/" >> /etc/sysupgrade.conf
echo "/usr/local/share/xray-assets/" >> /etc/sysupgrade.conf
echo "/etc/crontabs/root" >> /etc/sysupgrade.conf
```

Verify it was added:

```bash
grep -E "usr/local|crontabs" /etc/sysupgrade.conf
```

Should show:
```
/usr/local/sbin/
/usr/local/share/xray-assets/
/etc/crontabs/root
```

### 2. Create directories

```bash
mkdir -p /usr/local/sbin
chmod 0755 /usr/local/sbin
```

### 3. Install the script

Upload `runetfreedom-geodata-updater.sh` to the router:

```bash
# From your computer
scp runetfreedom-geodata-updater.sh root@192.168.1.1:/usr/local/sbin/runetfreedom-geodata-updater.sh

# On the router
chmod 0755 /usr/local/sbin/runetfreedom-geodata-updater.sh
```

Or create it manually:

```bash
vi /usr/local/sbin/runetfreedom-geodata-updater.sh
# Paste the script content
chmod 0755 /usr/local/sbin/runetfreedom-geodata-updater.sh
```

### 4. First run - Download geodata files
Test without making changes:
```bash
DRY_RUN=1 /usr/local/sbin/runetfreedom-geodata-updater.sh
```
or go ahead and download the geodata files and create symlinks:
```bash
# Run the script to download geodata files and create symlinks
/usr/local/sbin/runetfreedom-geodata-updater.sh
```
Verify files and symlinks were created:

```bash
# Check downloaded files
ls -lh /usr/local/share/xray-assets/
# Should show: geoip.dat, geosite.dat, backup/

# Check symlinks in /usr/share/xray/
ls -l /usr/share/xray/geo*.dat
# Should show symlinks pointing to /usr/local/share/xray-assets/
```

### 5. Schedule automatic updates (cron) - one-time action

The first time you run `crontab -e`, it will create `/etc/crontabs/root` (already added to sysupgrade.conf in step 1).

```bash
# Edit root's crontab (creates file if doesn't exist)
crontab -e

# Add this line (runs daily at 4:10 AM) with SHIFT+R and then paste from clipboard
10 4 * * * /usr/local/sbin/runetfreedom-geodata-updater.sh >/dev/null 2>&1

# Save and exit the editor
# For vi: press ESC, type :wq, press ENTER
```

Verify cron job was added:

```bash
crontab -l
# Should show: 10 4 * * * /usr/local/sbin/runetfreedom-geodata-updater.sh >/dev/null 2>&1

# Check file was created
ls -l /etc/crontabs/root
```

**Alternative: Direct file creation** (if crontab -e doesn't work):

```bash
# Create crontab file directly
mkdir -p /etc/crontabs
echo "10 4 * * * /usr/local/sbin/runetfreedom-geodata-updater.sh >/dev/null 2>&1" > /etc/crontabs/root

# Reload cron daemon
/etc/init.d/cron restart
```

### 6. Configure Xray DNS routing rules (optional but recommended)
Very important to understand the meaning of these DNS settings, these are NO any kind of smart mechanisms or a fallback system - the labels in UI are very misleading. That's just simple "bucket" with rules: if rule matches DNS server will be used, if doesn't match - skipped, until default server is hit, if nothing is specified in rules - skipped as well, until reaches default DNS.

if domain matches Blocked rules:
    return NXDOMAIN or loopback

else if domain matches Bypassed domain rules:
    resolve via Fast DNS - any dns server any rules

else if domain matches Forwarded domain rules:
    resolve via Secure DNS - any dns server any rules

else:
    resolve via Default DNS

Before switching to the new geodata, configure your routing rules:

**Via UCI (command line)**:

```bash
# Remove old category-ads if present (note: correct name is category-ads-all)
uci del_list xray_core.@general[0].blocked_domain_rules='geosite:category-ads' 2>/dev/null || true
uci del_list xray_core.@general[0].blocked_domain_rules='geosite:category-ads-all' 2>/dev/null || true

# Add ad blocking (optional but recommended)
uci add_list xray_core.@general[0].blocked_domain_rules='geosite:category-ads-all'

# Commit changes (don't restart yet!)
uci commit xray_core
```

**Custom Options** 
This ONLY is needed if you want to use specifically DoH as the DNS server(s), if normal DNS upstream on UDP:53 will be used (like 1.1.1.1:53 or 8.8.8.8:53, system uses 1.1.1.1:53 if it's left empty) then no custom options AT ALL are needed
It is done like that, as this version of Lucy UI for XRAY doeson't support DoH by default.
Maybe in future versions it will be natively supported. 
I also have used custom options for dns debugging and increasing loglevel along with disabling DNS cache. 
XRAY Has 
WARNING!!!!
If you will be using DoH in the DNS section it MUST be specified in a special format in custom options: "https+local" - otherwise it is automatically routed to the tunnel and that makes dns routing loop and it stops working. Here is the reference: https://xtls.github.io/config/dns.html#dnsobject

```javascript
return function(config) {
  if (type(config.dns) != "object")
    config.dns = {};

//temp for debugging
//config.dns.disableCache = true;
// temp for debugging ends
// UseIP or UseIPv4 ONLY if needed but sometimes helps 
// avoid getting AAAA IPv6 results from uplinks
//  config.dns.queryStrategy = "UseIPv4";

config.dns.servers = [
  { address: "https+local://cloudflare-dns.com/dns-query" },
  { address: "https+local://doh.sb/dns-query" },
  { address: "https+local://dns10.quad9.net/dns-query" },
  { address: "https+local://dns.google/dns-query" }
];

//temp for debugging
//  if (type(config.log) != "object")
//    config.log = {};
//  config.log.loglevel = "debug";
//  config.log.dnsLog = false;
// temp for debugging ends

  return config;
};
```
After that must manually check if we didn't break Xray - must return Configuration: OK

# Stop Xray and revert its injected dnsmasq/fw4 bits
/etc/init.d/xray_core stop

# Hard kill if anything is left
killall -q xray 2>/dev/null || true

# Optional: clean generated artifacts (safe)
rm -rf /var/etc/xray/*

# Restart base services
/etc/init.d/dnsmasq restart
/etc/init.d/firewall restart

# Start Xray again (will regenerate config + nft include)
 /etc/init.d/xray_core start

# Quick sanity checks
/etc/init.d/xray_core status
/usr/bin/xray run -test -confdir /var/etc/xray | head -50


**Via LuCI web interface**:

1. Go to **Services** → **Xray** → **DNS**
2. **Blocked Domain Rules**: Add `geosite:category-ads-all`
3. **Bypassed Domain Rules**: Add whatever rules you see fit - for example I want all local Russian domains to skip proxy and to be accessed directly, I specified the following rules:
Fast DNS
5.141.95.250
Bypassed domain rules
domain:server.tunnelpublic.com
domain:doh.sb
domain:cloudflare-dns.com
geosite:category-ru
geosite:ru-available-only-inside
geoip:ru
last 3 are a safety measure to avoid routing loops, like if we need to resolve dns but if DoH is used we need to resolve dns FQDN and we can't as it must be first resolved to IP to be able to send DoH request to resolve other hosts. It is called DNS bootstrap. 
4. Go to **Services** → **Xray** → **Outbound Routing**
5. **GeoIP Direct Code List (IPv4)**: `RU`
6. **GeoIP Direct Code List (IPv6)**: `RU`
7. **Bypassed IP**:
`192.168.0.0/16`
`82.XX.XX.XX`
`127.0.0.0/8`
`::1/128`
here I specified loopback addresses my local LAN and Public IP of my actual XRAY public Internet server
Requests to these IPs won't be forwarded through Xray.
8. **Bypassed TCP Ports**
`49665`
`853`
Requests to these TCP Ports won't be forwarded through Xray.
9. **Bypassed UDP Ports**
`49665`
`53`
`443`
`123`
Requests to these UDP Ports won't be forwarded through Xray.
Here port 53 **MUST** be present otherwise DNS might not work at all at is could be looped to the tunnel.
10. **Save & Apply** to commin all changes and restart the services.

### 6.1. Configure Remote Xray Server Hostname Resolution (if using FQDN)

**IMPORTANT**: If your Xray uplink server uses a domain name (FQDN) instead of a static IP address, you **MUST** configure hostname resolution to avoid DNS recursion loops.

**Why this is needed:**
- Without this configuration, Xray will try to resolve the server hostname through its own DNS system and\or witn DNS routing. 
- This creates a recursion: Xray needs to connect to resolve DNS → but needs DNS to connect → infinite loop
- By specifying direct DNS resolution for the server hostname, we break this loop

**Configuration via LuCI**:

1. Go to **Services** → **Xray** → **General Settings** tab → **Xray Servers** section below
2. Click **Edit** on your existing server entry
3. Select the **Server Hostname Resolving** tab
4. Configure the following settings:

   **Domain Strategy**: `UseIP`
   - Whether to use IPv4 or IPv6 address if Server Hostname is a domain

   **Resolve Domain via DNS**: `DNS_IP_ADDRESS_HERE`
   - Specify a DNS IP to resolve server hostname
   - **CRITICAL**: Use a public direct DNS server (not DoH)
   - This forces the system to resolve the remote tunnel server IP via specific DNS directly

   **Resolve Domain DNS Method**: `UDP`
   - Effective when DNS above is set
   - Direct methods will bypass Xray completely so it won't get blocked
   - **DO NOT** use DoH here - it will create a routing loop

   **Expected Server IPs**: *(leave empty unless you want IP filtering)*
   - Optional: Filter resolved IPs by GeoIP or CIDR
   - Requires geoip.dat resource file for GeoIP filtering

5. **Save and Close**

**Verification**:

```bash
# Check that Xray can resolve and connect to the server
/etc/init.d/xray_core status

# Watch logs for connection attempts
logread -f | grep xray

# Verify the server hostname resolves correctly
nslookup your-server.example.com 5.141.95.250
```

**Common mistakes to avoid:**
- Using DoH (https+local://) for server hostname resolution → creates routing loop
- Not configuring this when using FQDN → DNS recursion, connection fails
- Using the same DNS as in the main DNS config → may cause routing issues

### 7. Verify runetfreedom geodata is working

```bash
# Check symlinks are pointing to xray-assets
ls -l /usr/share/xray/geo*.dat
# Should show:
# geoip.dat -> /usr/local/share/xray-assets/geoip.dat
# geosite.dat -> /usr/local/share/xray-assets/geosite.dat

# Check geodata files exist
ls -lh /usr/local/share/xray-assets/*.dat

# Verify ru-blocked category exists in geosite.dat
strings /usr/local/share/xray-assets/geosite.dat | grep ru-blocked

# Check Xray logs for geodata loading
logread | grep -i "geosite\|geoip"

# Test DNS resolution through Xray (drill must be installed)
drill @127.0.0.1 -p 5300 youtube.com
```

### Manual execution

```bash
# Normal run
/usr/local/sbin/runetfreedom-geodata-updater.sh

# Dry-run mode (no changes)
DRY_RUN=1 /usr/local/sbin/runetfreedom-geodata-updater.sh

```

### View logs

The script logs to syslog with tag `runetfreedom` (priorities: info/warn/error).

```bash
# Recent logs (last 50 lines)
logread -e runetfreedom | tail -n 50

# Follow logs in real-time (useful during manual runs)
logread -f -e runetfreedom

# All logs from last run
logread | grep runetfreedom

# Filter by log level
logread | grep 'runetfreedom.*ERROR'   # Errors only
logread | grep 'runetfreedom.*WARN'    # Warnings only

# Check if last update succeeded
logread -e runetfreedom | grep -E "Update completed|already up-to-date"
```

**Log output example:**
```
Dec 27 04:10:01 router user.info runetfreedom: Starting runetfreedom geodata update
Dec 27 04:10:02 router user.info runetfreedom: Detected service: xray_core
Dec 27 04:10:02 router user.info runetfreedom: Directories ready: /usr/local/share/xray-assets
Dec 27 04:10:03 router user.info runetfreedom: Checking for updates...
Dec 27 04:10:03 router user.info runetfreedom: All geodata files are already up-to-date, nothing to do
```

### The script survives firmware upgrades and restore (or at least it should - I haven't checked :) )

Files included in sysupgrade backups:
- `/usr/local/sbin/` (script)
- `/usr/local/share/xray-assets/` (geodata + backups)
- `/etc/crontabs/root` (cron schedule)

## Troubleshooting

### Update fails with "Another update is running"

```bash
# Remove stale lock
rmdir /tmp/.runetfreedom-geodata.lock
```

### Config validation fails

Check Xray test output:
```bash
/usr/bin/xray run -test -confdir /var/etc/xray | head -50
```
The script automatically rolls back to the previous geodata on validation failure.

### Xray won't start after update

```bash
# Check service status
/etc/init.d/xray_core status

# View detailed logs
logread | grep -E 'xray|runetfreedom'

# Manually restore from backup
cp /usr/local/share/xray-assets/backup/geoip.dat.* /usr/local/share/xray-assets/geoip.dat
cp /usr/local/share/xray-assets/backup/geosite.dat.* /usr/local/share/xray-assets/geosite.dat
/etc/init.d/xray_core restart
```

### Download fails

```bash
# Test connectivity to GitHub
wget -O /dev/null https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release/geoip.dat.sha256sum

# Check DNS resolution
nslookup raw.githubusercontent.com
```

### Geodata seems old

```bash
# Check last update
ls -l /usr/local/share/xray-assets/*.dat

# Force update
/usr/local/sbin/runetfreedom-geodata-updater.sh
```

## Exit Codes

- `0` - Success
- `1` - Generic failure (missing dependencies, permission denied, etc.)
- `2` - Download or verification failure
- `3` - Config validation failed (automatic rollback performed)
- `4` - Service restart or health check failed

## License

This script is provided as-is for use with OpenWrt and Xray on GL.iNet GL-MT6000 routers.

## References

- [RuNetFreedom Russia V2Ray Rules](https://github.com/runetfreedom/russia-v2ray-rules-dat)
- [V2Fly Domain List Community](https://github.com/v2fly/domain-list-community)
- [Xray-core Configuration Reference](https://xtls.github.io/config/)
- [OpenWrt Documentation](https://openwrt.org/docs/start)
- [openwrt-xray GitHub](https://github.com/yichya/openwrt-xray)
- [luci-app-xray GitHub](https://github.com/yichya/luci-app-xray)

## See Also

- [xray_udp_transparent_proxy_advanced_routing.md](xray_udp_transparent_proxy_advanced_routing.md) - Advanced UDP transparent proxy configuration for messaging app voice/video calls (Telegram, WhatsApp, FaceTime, Discord)
