# Xray Health Monitor for OpenWRT

Automated health monitoring and failover system for Xray tunnels on OpenWRT routers.

---

## ⚠️ IMPORTANT: Configuration Required

**Before using this script, you MUST configure your Xray server details in `check-xray.sh`:**

```bash
# Edit lines 54-57 in check-xray.sh:
TUNNEL_FQDN="your-xray-server.example.com"  # Your Xray server hostname/FQDN
TUNNEL_IP="1.2.3.4"                         # Your Xray server IP address
TUNNEL_PORT="443"                            # Your Xray server port
```

**Replace the placeholder values with your actual server details:**
- `TUNNEL_FQDN`: Your Xray server's hostname (e.g., `vpn.mydomain.com`)
- `TUNNEL_IP`: Your Xray server's IP address (e.g., `203.0.113.42`)
- `TUNNEL_PORT`: Your Xray server's port (typically `443` for VLESS+REALITY)

The script monitors this server for connectivity and triggers failover when it's unreachable.

---

## Why This Was Created

When running Xray tunnels for privacy and bypassing restrictions, several scenarios can cause connectivity issues:

- **Tunnel server goes down** - Your VPS provider has an outage or the xray server crashes
- **Tunnel gets blocked** - ISP or government blocks your tunnel server IP/port
- **DNS resolution failures** - DoH or DNS servers become unreachable
- **Network interruptions** - Temporary network issues that xray doesn't recover from automatically

Without monitoring, your entire network loses internet connectivity until you manually intervene. This system solves that problem by:

1. **Continuously monitoring** tunnel health every 4 minutes
2. **Automatically failing over** to direct internet when tunnel is down
3. **Auto-restoring** the tunnel when it comes back online
4. **Distinguishing** between tunnel failures and ISP outages
5. **Displaying** real-time status in LuCI web interface

---

## What It Can Do

### Core Features

✅ **Intelligent Health Monitoring**
- Checks ISP/WAN connectivity (if PPPoE exists, then its interface status + DNS pings)
- Verifies xray process is running
- Validates xray DNS listener ports (5300) are active
- Distinguishes between ISP outages vs tunnel failures

✅ **Automatic Failover**
- Detects tunnel failure within 4 minutes
- Stops xray and removes nftables interception rules
- Switches to direct internet routing via ISP DNS
- Network stays online during tunnel outages

✅ **Automatic Recovery**
- Retries tunnel restoration every 15 minutes (first 3 hours)
- Then retries hourly for up to 7 days
- Automatically restores when tunnel comes back
- Returns to normal operation seamlessly

✅ **Persistent State Management**
- Survives router reboots and power losses
- Remembers failover state across restarts
- Resumes retry schedule after unclean shutdowns
- State stored in `/etc/xray-health.state`

✅ **Comprehensive Logging**
- Persistent logs: `/root/xray-health-persistent.log` (survives reboots)
- System logs: `logread -e 'check-xray'` (syslog integration)
- Weekly log rotation with configurable 4-week retention + gzip compression

✅ **LuCI Web Interface Integration**
- Real-time status widget on Status → Overview page
- Displays both **Xray Tunnel** and **ISP Internet** status
- Color-coded indicators (Green=Connected, Red=Down, Gray=Checking)
- Auto-refreshes every 4 minutes
- Hover tooltips with detailed status messages

---

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│ 1. check-xray.sh daemon (runs every 4 minutes)         │
│    - Checks PPPoE/WAN interface status via ubus        │
│    - Pings ISP DNS (5.141.95.250) + Yandex DNS         │
│    - Verifies xray process running (pidof xray)        │
│    - Validates DNS port 5300 listening (ss/netstat)    │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│ 2. Health Status Decision Tree                         │
│                                                         │
│    ISP Down? → Wait for ISP recovery (no failover)     │
│    Xray OK?  → Continue monitoring                     │
│    Xray Bad? → Initiate failover to direct internet    │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│ 3. Failover Actions                                     │
│    - Stop xray: /etc/init.d/xray_core stop             │
│    - Disable autostart: /etc/init.d/xray_core disable  │
│    - Restart firewall: removes nftables DNS intercept  │
│    - Restart dnsmasq: DNS flows to upstream ISP        │
│    - Write state: /etc/xray-health.state (mode=direct) │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│ 4. Recovery Monitoring                                  │
│    - Retry every 15 min (first 3 hours)                │
│    - Then retry hourly (up to 7 days)                  │
│    - Test: Start xray → Check ports → Validate status  │
│    - Success? → Restore tunnel + clear failover state  │
└─────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────┐
│ 5. Status Display in LuCI                              │
│    - Daemon writes: /tmp/run/xray-health/xray.status   │
│    - RPC backend: /usr/libexec/rpcd/xray-health        │
│    - ubus call: xray-health.getStatus                  │
│    - Widget polls: every 4 minutes via AJAX            │
│    - Shows: Xray Tunnel + Internet status side-by-side │
└─────────────────────────────────────────────────────────┘
```

### DNS Management

**Critical Understanding:** Xray manages DNS interception at the **firewall layer**, NOT at the dnsmasq configuration layer.

- **Normal Operation (Xray Running):**
  - nftables rules redirect port 53 → `127.0.0.1:5300-5303` (xray DoH or DNS listeners)
  - Xray forwards DNS to encrypted DoH servers (cloudflare-dns.com, dns.google) or any UDP normal DNS servers if configured
  - dnsmasq upstream servers in the dnsmasq servers remain configured but unused, and are only used by the firewall itself.

- **Failover Mode (Xray Stopped):**
  - Firewall restart removes nftables interception rules
  - DNS traffic flows directly to dnsmasq upstream servers
  - Normal unencrypted DNS resolution via ISP

**The script does NOT modify `/etc/config/dhcp`** - it only restarts services to apply/remove firewall rules.

---

## Installation

### Prerequisites

- OpenWRT 23.x or 24.x
- Xray-core package installed (`xray-core`)
- LuCI app for Xray installed (`luci-app-xray`)
- PPPoE or WAN internet connection
- SSH access to router

### Step 1: Copy Scripts to Router

From your PC (where you have the scripts):

```bash
# Copy both scripts to router
scp check-xray.sh install-xray-health-widget.sh root@192.168.1.1:/usr/local/sbin/

# SSH to router
ssh root@192.168.1.1
```

On the router:

```bash
# Make scripts executable
chmod +x /usr/local/sbin/*.sh
```

### Step 2: Run Installation Script

```bash
# Install LuCI widget, RPC backend, and init.d service
/usr/local/sbin/install-xray-health-widget.sh install
```

This installs:
- LuCI widget: `/www/luci-static/resources/view/status/include/00_xray_health.js`
- RPC backend: `/usr/libexec/rpcd/xray-health`
- ACL permissions: `/usr/share/rpcd/acl.d/luci-app-xray-health.json`
- Init.d service: `/etc/init.d/xray-health`

### Step 3: Restart Services

```bash
# Restart RPC and web server
/etc/init.d/rpcd restart
/etc/init.d/uhttpd restart
```

### Step 4: Enable and Start Daemon

```bash
# Enable autostart on boot
/etc/init.d/xray-health enable

# Start monitoring daemon
/etc/init.d/xray-health start

# Verify it's running
/etc/init.d/xray-health status
```

### Step 5: Verify LuCI Widget

1. Open LuCI web interface: `http://192.168.1.1`
2. Navigate to: **Status → Overview**
3. Hard refresh browser: **Ctrl+F5** (clear cache)
4. You should see:
   ```
   Xray Tunnel: [Green: Tunnel Active]    Internet: [Green: Connected]
   ```

### Step 6: Add to Firmware Persistence (Optional but Recommended)

To preserve scripts and state across firmware updates:

```bash
# Edit sysupgrade config
vi /etc/sysupgrade.conf

# Add these lines:
/usr/local/sbin/check-xray.sh
/usr/local/sbin/install-xray-health-widget.sh
/etc/xray-health.state
/etc/xray-health-logrotate
/root/xray-health-persistent.log*
```

---

## Usage

### Normal Operation

Once installed and started, the daemon runs automatically in the background. No manual intervention needed.

**What happens during normal operation:**
- Daemon checks tunnel health every 4 minutes
- Status shows: `Xray Tunnel: Tunnel Active` + `Internet: Connected`
- Logs minimal activity (only status changes)

**What happens when tunnel fails:**
- Daemon detects failure within 4 minutes
- Automatically stops xray and switches to direct internet
- Status shows: `Xray Tunnel: Tunnel Down` + `Internet: Connected`
- Network stays online via ISP direct routing
- Daemon retries restoration every 15 minutes (first 3 hours), then hourly

**What happens when tunnel recovers:**
- Daemon detects tunnel is back online
- Automatically restores xray and enables nftables interception
- Status returns to: `Xray Tunnel: Tunnel Active` + `Internet: Connected`

### Manual Commands

#### Check Daemon Status

```bash
# Check if daemon is running
/etc/init.d/xray-health status
```

#### Stop/Start/Restart Daemon

```bash
# Stop monitoring
/etc/init.d/xray-health stop

# Start monitoring
/etc/init.d/xray-health start

# Restart monitoring
/etc/init.d/xray-health restart
```

#### Manual Restore (With Connectivity Check)

If you want to manually restore tunnel after fixing server issues:

```bash
# This will:
# 1. Enable and start xray
# 2. Restart firewall and DNS
# 3. Test tunnel connectivity
# 4. Report success or failure

/usr/local/sbin/check-xray.sh --restore
# or
/usr/local/sbin/check-xray.sh restore
```

Exit codes:
- `0` = Success (tunnel restored and working)
- `1` = Failure (tunnel not reachable or xray failed to start)

#### Force Restore (Without Connectivity Check)

If tunnel test fails but you know xray should work:

```bash
# This will:
# 1. Enable and start xray
# 2. Restart firewall and DNS
# 3. Skip connectivity test
# 4. Always succeed

/usr/local/sbin/check-xray.sh --force-restore
# or
/usr/local/sbin/check-xray.sh force-restore
```

This is useful when:
- Tunnel server is temporarily unreachable but you want xray enabled
- You're troubleshooting and want to force a state change
- You prefer manual verification over automated checks

## Logging

### Log Files

| File | Purpose | Persistence | Rotation |
|------|---------|-------------|----------|
| `/root/xray-health-persistent.log` | Status changes and events | Survives reboots | Weekly, 4 weeks kept |
| `/root/xray-health-persistent.log.YYYY-MM-DD.gz` | Rotated archives | Survives reboots | Auto-deleted after 4 weeks |
| System log (syslog) | All events | Volatile | Standard syslog rotation |

### Viewing Logs

**Real-time monitoring:**
```bash
# System logs
logread -f -e 'check-xray'
```

**Recent events:**
```bash
# Last 50 lines from syslog
logread -e 'check-xray' | tail -50
```

**Search for specific events:**
```bash
# Find failover events
logread -e 'check-xray' | grep "FAILOVER"

# Find restore events
logread -e 'check-xray' | grep "RESTORE"

# Find errors
logread -e 'check-xray' | grep "ERROR"

# Check ISP connectivity issues
logread -e 'check-xray' | grep -i "isp\|wan\|pppoe"
```

**Analyze rotated logs:**
```bash
# List all archived logs
ls -lh /root/xray-health-persistent.log*

# View compressed log
zcat /root/xray-health-persistent.log.2026-01-01.gz | less

# Search in compressed log
zgrep "FAILOVER" /root/xray-health-persistent.log.2026-01-01.gz
```

### Log Rotation

Logs automatically rotate weekly (every 7 days):
- Current log is moved to dated archive: `xray-health-persistent.log.YYYY-MM-DD`
- Archive is compressed with gzip (saves ~90% space)
- Logs older than 4 weeks are automatically deleted
- Rotation state tracked in `/etc/xray-health-logrotate`

Manual rotation:
```bash
# Logs rotate automatically, but you can force a new log:
killall check-xray.sh
mv /root/xray-health-persistent.log /root/xray-health-persistent.log.manual-backup
/etc/init.d/xray-health start
```

---

## Troubleshooting

### Daemon Won't Start

**Symptom:** `/etc/init.d/xray-health status` shows "not running"

**Solution:**
```bash
# Check for errors in logs
logread -e 'check-xray' | tail -20

# Try running manually to see errors
/usr/local/sbin/check-xray.sh

# Common issues:
# 1. PID file conflict - remove it
rm -f /var/run/xray-health.pid

# 2. State file corruption - remove it
rm -f /etc/xray-health.state

# 3. Status directory missing - create it
mkdir -p /tmp/run/xray-health

# Then restart
/etc/init.d/xray-health start
```

### Widget Not Showing in LuCI

**Symptom:** No status widget on Status → Overview page

**Solution:**
```bash
# 1. Verify widget file exists
ls -la /www/luci-static/resources/view/status/include/00_xray_health.js

# 2. Check RPC backend
ubus list | grep xray-health

# 3. Test RPC backend
ubus call xray-health getStatus

# 4. Reinstall if missing
/usr/local/sbin/install-xray-health-widget.sh install
/etc/init.d/rpcd restart
/etc/init.d/uhttpd restart

# 5. Hard refresh browser
# Ctrl+F5 or Ctrl+Shift+R
```

### Daemon Fails Over Immediately (Tunnel is Actually Working)

**Symptom:** Daemon keeps detecting "tunnel failure" even though you can browse normally

**Diagnosis:**
```bash
# Check logs to see why it thinks tunnel is down
logread -e 'check-xray' | tail -50

# Common causes:
# 1. Xray DNS ports not listening yet (takes 5-10 seconds after start)
# 2. curl test timing out (was issue, now fixed)
```

**Solution:**
```bash
# Force restore and check status
/usr/local/sbin/check-xray.sh force-restore

# Wait 4 minutes for daemon to check again
sleep 240

# Verify status
ubus call xray-health getStatus
```

### ISP Internet Down But Daemon Keeps Trying to Restore Xray

**Symptom:** Logs show "ISP internet down" but daemon still attempts restoration

**This is normal behavior** - the daemon will:
1. Detect ISP is down (`diag_result=2`)
2. NOT fail over (keeps xray enabled if it was enabled)
3. Wait for ISP to recover
4. Update status: `Internet: Disconnected`

**Solution:** Wait for ISP to recover. Daemon won't make changes during ISP outages.

### Tunnel Keeps Failing After Restoration

**Symptom:** Daemon restores tunnel but it immediately fails again

**Diagnosis:**
```bash
# 1. Check if xray is actually starting
ps | grep xray

# 2. Check xray logs
logread -e 'xray' | tail -50

# 3. Check DNS port
ss -lun | grep 5300

# 4. Test connectivity from LAN client (not router)
# From your PC: curl https://www.google.com
```

**Common causes:**
- Tunnel server is actually down (check VPS)
- Tunnel server IP/port blocked (check firewall, ISP blocking)
- DNS DoH servers unreachable (check xray config)
- Xray configuration error (validate config)

**Solution:**
```bash
# 1. Validate xray config
/usr/bin/xray run -test -confdir /var/etc/xray

# 2. Manually test tunnel server reachability
ping -c 3 <your-xray-server>
nc -zv <your-xray-server> <<your-xray-server-port>

# 3. Check xray service status
/etc/init.d/xray_core status

# 4. Manually restart xray
/etc/init.d/xray_core restart

# 5. If still failing, keep in direct mode
killall check-xray.sh
/etc/init.d/xray_core stop
/etc/init.d/xray_core disable
```

### Daemon Exits with "Daemon already running" Error

**Symptom:** Service starts but immediately exits with PID conflict

**Solution:**
```bash
# 1. Kill any existing instances
killall check-xray.sh

# 2. Remove stale PID file
rm -f /var/run/xray-health.pid

# 3. Restart service
/etc/init.d/xray-health start

# 4. Verify
/etc/init.d/xray-health status
```

### State File Corruption After Power Loss

**Symptom:** Daemon behaves erratically after router power loss/reboot

**Solution:**
```bash
# The daemon has unclean shutdown recovery, but if state is corrupted:

# 1. Check current state
cat /etc/xray-health.state

# 2. Remove corrupted state
rm -f /etc/xray-health.state

# 3. Manually verify xray status
/etc/init.d/xray_core status
ps | grep xray

# 4. Force restore to known good state
/usr/local/sbin/check-xray.sh force-restore

# 5. Restart daemon
/etc/init.d/xray-health restart
```
---

## Uninstallation

To completely remove the xray health monitoring system:

```bash
# 1. Stop and disable service
/etc/init.d/xray-health stop
/etc/init.d/xray-health disable

# 2. Run uninstaller
/usr/local/sbin/install-xray-health-widget.sh uninstall

# 3. Remove scripts
rm -f /usr/local/sbin/check-xray.sh
rm -f /usr/local/sbin/install-xray-health-widget.sh

# 4. Remove state and logs
rm -f /etc/xray-health.state
rm -f /etc/xray-health-logrotate
rm -f /root/xray-health-persistent.log*
rm -rf /tmp/run/xray-health

# 5. Remove from sysupgrade config (if added)
vi /etc/sysupgrade.conf
# Delete lines mentioning xray-health

# 6. Restart services
/etc/init.d/rpcd restart
/etc/init.d/uhttpd restart

# 7. Hard refresh browser (Ctrl+F5)
```

---

## Advanced Configuration

### Adjusting Check Intervals

Edit `/usr/local/sbin/check-xray.sh` and modify these variables:

```bash
# Timing configuration (in seconds)
CHECK_INTERVAL_NORMAL=240            # How often to check when healthy (default: 4 min)
CHECK_INTERVAL_SHORT=900             # Retry interval first 3 hours (default: 15 min)
CHECK_INTERVAL_LONG=3600             # Retry interval after 3 hours (default: 1 hour)
SHORT_INTERVAL_DURATION=10800        # How long to use short interval (default: 3 hours)
MAX_RETRY_DURATION=604800            # Total retry duration (default: 7 days)
```

After changes:
```bash
/etc/init.d/xray-health restart
```

### Customizing Tunnel Configuration

If your tunnel uses different settings, edit these variables:

```bash
# Tunnel server configuration
TUNNEL_FQDN="xray.mytest.com"        # Your tunnel server domain
TUNNEL_IP="X.X.X.X"          # Your tunnel server IP
TUNNEL_PORT="443"                 # Your tunnel port

# Network configuration
LAN_IFACE="br-lan"                # Your LAN interface

# Upstream ISP connectivity test targets
UPSTREAM_DNS="X.X.X.X"       # Your ISP DNS
UPSTREAM_TEST_HOST="X.X.X.X"    # Reliable public DNS for testing
```

### Widget Refresh Interval

Edit `/www/luci-static/resources/view/status/include/00_xray_health.js`:

```javascript
// Change poll interval (milliseconds)
poll.add(callXrayHealthStatus, 240);  // Default: 240 seconds (4 minutes)
```

After changes:
```bash
# Clear browser cache with Ctrl+F5
```

---

## FAQ

**Q: Will this work with other proxy systems (V2Ray, Shadowsocks, etc.)?**  
A: Not directly. This is specifically designed for Xray with luci-app-xray. However, the general approach could be adapted.

**Q: Does this consume significant router resources?**  
A: No. The daemon uses ~1-2 MB RAM and minimal CPU (<1%). Checks run only once every 4 minutes.

**Q: What happens if the daemon crashes?**  
A: Procd (OpenWRT's process manager) automatically restarts it (up to 3 times per hour). Persistent state ensures no data loss.

**Q: Can I run this on multiple WAN connections (load balancing)?**  
A: Currently supports single WAN. Multi-WAN scenarios would need script modifications. (feel free to create pull request)

**Q: Does failing over to direct internet change my firewall rules permanently?**  
A: No. Failover only restarts firewall to remove xray's nftables rules. Restoration re-applies them. Your static firewall config (`/etc/config/firewall`) is never modified.

**Q: Can I disable automatic restoration and only use manual restore?**  
A: Yes. Stop the daemon:
```bash
/etc/init.d/xray-health stop
/etc/init.d/xray-health disable
```
Now use manual commands:
```bash
# Force failover
/etc/init.d/xray_core stop
/etc/init.d/xray_core disable
/etc/init.d/firewall restart

# Force restore
/usr/local/sbin/check-xray.sh force-restore
```

**Q: Why does the script check both xray process AND ports?**  
A: Because xray can be "running" (process exists) but not fully initialized (DNS ports not listening). This ensures the tunnel is actually functional, not just started.

**Q: What if I want email/Telegram notifications when failover happens?**  
A: This would require additional scripts. You could modify `failover_to_direct()` and `restore_tunnel()` functions to call notification commands. Example:
```bash
# In failover_to_direct(), add:
curl -X POST https://api.telegram.org/bot<TOKEN>/sendMessage \
     -d chat_id=<CHAT_ID> \
     -d text="Xray tunnel failed over to direct internet"
```

---

## Credits

Created for OpenWRT routers running Xray VLESS+REALITY tunnels.

Tested on:
- OpenWRT 23.05.x / 24.10.x
- Xray-core 1.8.x
- luci-app-xray

---

**Remember:** This script monitors xray health, it doesn't fix xray configuration issues. If manual xray operations fail, fix xray first.
