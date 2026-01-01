#!/bin/sh
# Xray Health Monitor Daemon
# Monitors xray tunnel connectivity and automatically fails over to direct internet when unreachable
# Displays status on OpenWRT LuCI Status Overview page
#
# INSTALLATION:
#   1. Copy this script to /usr/local/sbin/check-xray.sh
#   2. Make executable: chmod +x /usr/local/sbin/check-xray.sh
#   3. Run install-xray-health-widget.sh to install LuCI widget
#   4. Enable service: /etc/init.d/xray-health enable && start
#
# FIRMWARE UPDATE PERSISTENCE:
#   To preserve state across firmware updates, add to /etc/sysupgrade.conf:
#     /usr/local/sbin/check-xray.sh
#     /etc/xray-health.state
#     /etc/xray-health-logrotate
#     /root/xray-health-persistent.log*
#   Note: Files in /usr/local/sbin/ and /etc/ (overlay) typically survive sysupgrade
#         but explicitly listing them in /etc/sysupgrade.conf ensures preservation.
#   No restart needed - /etc/sysupgrade.conf is read only during firmware upgrade.
#
# USAGE:
#   ./check-xray.sh                    - Start daemon (monitors tunnel health)
#   ./check-xray.sh --restore          - Restore xray and verify connectivity
#   ./check-xray.sh --force-restore    - Restore xray WITHOUT connectivity check (always succeeds)
#
# MANUAL OVERRIDE / SHUTDOWN:
#   To stop monitoring and keep current state:
#     killall check-xray.sh
#
#   To stop monitoring and restore xray services:
#     killall check-xray.sh && ./check-xray.sh --restore
#
#   To force direct internet (disable xray):
#     killall check-xray.sh
#     /etc/init.d/xray_core stop
#     /etc/init.d/xray_core disable
#
# FILES:
#   /var/run/xray-health.pid                - Daemon PID file (volatile)
#   /etc/xray-health.state                  - Current state (persistent - survives reboots)
#   /etc/xray-health-logrotate              - Last log rotation timestamp
#   /root/xray-health-persistent.log        - Current persistent log (rotated weekly)
#   /root/xray-health-persistent.log.YYYY-MM-DD.gz - Rotated/compressed logs (kept 4 weeks)
#   /tmp/run/xray-health/xray.status        - JSON status for LuCI widget (volatile)
#
# SYSTEM LOGS:
#   View real-time logs:  logread -f -e 'check-xray'
#   View recent logs:     logread -e 'check-xray'

###############################################################################
# CONFIGURATION
###############################################################################

# Tunnel server configuration - CHANGE THESE TO YOUR SERVER DETAILS
TUNNEL_FQDN="your-xray-server.example.com"  # Your Xray server hostname/FQDN
TUNNEL_IP="1.2.3.4"                         # Your Xray server IP address
TUNNEL_PORT="443"                            # Your Xray server port

# Network configuration
LAN_IFACE="br-lan"

# Upstream ISP connectivity test targets (for verifying basic internet)
UPSTREAM_DNS="5.141.95.250"           # ISP DNS
UPSTREAM_TEST_HOST="77.88.8.8"        # Yandex public DNS (Russian, unlikely to be blocked)

# Timing configuration (in seconds)
CHECK_INTERVAL_NORMAL=240            # 4 minutes when tunnel is healthy
CHECK_INTERVAL_SHORT=900             # 15 minutes for first 3 hours of outage
CHECK_INTERVAL_LONG=3600             # 1 hour after 3 hours of outage
SHORT_INTERVAL_DURATION=10800        # 3 hours (12 attempts at 15min)
MAX_RETRY_DURATION=604800            # 7 days total before giving up
LOG_ROTATION_INTERVAL=604800         # 7 days (weekly log rotation)
LOG_KEEP_WEEKS=4                     # Keep 4 weeks of rotated logs

# File paths
PID_FILE="/var/run/xray-health.pid"
STATE_FILE="/etc/xray-health.state"                 # Persistent - survives reboots
PERSISTENT_LOG="/root/xray-health-persistent.log"   # Persistent log (rotated weekly)
LOG_ROTATION_STATE="/etc/xray-health-logrotate"     # Track last rotation time
STATUS_DIR="/tmp/run/xray-health"
STATUS_FILE="${STATUS_DIR}/xray.status"

###############################################################################
# UTILITY FUNCTIONS
###############################################################################

log_msg() {
    local msg="$1"
    local timestamp
    timestamp="[$(date '+%Y-%m-%d %H:%M:%S')]"
    
    # Write to persistent log
    echo "${timestamp} $msg" >> "$PERSISTENT_LOG"
    
    # Log to syslog via logger (without -s to avoid procd duplication)
    logger -t "check-xray" "$msg"
}

rotate_logs() {
    if [ ! -f "$PERSISTENT_LOG" ]; then
        return 0
    fi
    
    local current_time
    current_time="$(date +%s)"
    local last_rotation=0
    
    # Check when we last rotated
    if [ -f "$LOG_ROTATION_STATE" ]; then
        last_rotation=$(cat "$LOG_ROTATION_STATE" 2>/dev/null || echo 0)
    fi
    
    local time_since_rotation=$((current_time - last_rotation))
    
    # Rotate if more than LOG_ROTATION_INTERVAL has passed
    if [ $time_since_rotation -ge $LOG_ROTATION_INTERVAL ]; then
        local rotation_date
        rotation_date="$(date '+%Y-%m-%d')"
        local rotated_log="${PERSISTENT_LOG}.${rotation_date}"
        
        log_msg "Rotating persistent log (weekly rotation)"
        
        # Move current log to dated archive
        mv "$PERSISTENT_LOG" "$rotated_log"
        
        # Compress the rotated log to save space
        if command -v gzip >/dev/null 2>&1; then
            gzip -f "$rotated_log" && \
                log_msg "Compressed rotated log: ${rotated_log}.gz"
        fi
        
        # Clean up old rotated logs (keep only last LOG_KEEP_WEEKS weeks)
        local log_dir
        local log_base
        log_dir="$(dirname "$PERSISTENT_LOG")"
        log_base="$(basename "$PERSISTENT_LOG")"
        
        # Find and remove old log files (older than LOG_KEEP_WEEKS * 7 days)
        find "$log_dir" -name "${log_base}.*" -type f -mtime +$((LOG_KEEP_WEEKS * 7)) -delete 2>/dev/null
        
        # Update rotation timestamp
        echo "$current_time" > "$LOG_ROTATION_STATE"
        
        # Start fresh log
        log_msg "Log rotation complete - new log started"
    fi
}

die() {
    local exit_code="$1"
    shift
    log_msg "ERROR: $*"
    cleanup
    exit "$exit_code"
}

cleanup() {
    log_msg "Cleaning up..."
    rm -f "$PID_FILE"
    update_status -1 "stopped" -1 "unknown"
}

# Signal handlers
trap 'log_msg "Received SIGTERM, shutting down..."; cleanup; exit 0' TERM
trap 'log_msg "Received SIGINT, shutting down..."; cleanup; exit 0' INT

###############################################################################
# DEPENDENCY CHECKS
###############################################################################

check_dependencies() {
    log_msg "Checking required dependencies..."
    
    local missing_deps=""
    local optional_deps=""
    
    # Check critical dependencies
    if ! command -v ping >/dev/null 2>&1; then
        missing_deps="${missing_deps} ping"
    fi
    
    if ! command -v pidof >/dev/null 2>&1; then
        missing_deps="${missing_deps} pidof"
    fi
    
    # Check for at least one remote connectivity test tool
    local has_connectivity_tool=0
    if command -v nc >/dev/null 2>&1; then
        log_msg "  ✓ nc (netcat) - available for remote server checks"
        has_connectivity_tool=1
    else
        optional_deps="${optional_deps} nc"
    fi
    
    if command -v curl >/dev/null 2>&1; then
        log_msg "  ✓ curl - available for remote server checks"
        has_connectivity_tool=1
    else
        optional_deps="${optional_deps} curl"
    fi
    
    if command -v wget >/dev/null 2>&1; then
        log_msg "  ✓ wget - available for remote server checks"
        has_connectivity_tool=1
    else
        optional_deps="${optional_deps} wget"
    fi
    
    # Check for port listing tools
    if command -v ss >/dev/null 2>&1; then
        log_msg "  ✓ ss - available for port checks"
    elif command -v netstat >/dev/null 2>&1; then
        log_msg "  ✓ netstat - available for port checks"
    else
        missing_deps="${missing_deps} ss/netstat"
    fi
    
    # Check for ubus (OpenWRT specific)
    if command -v ubus >/dev/null 2>&1; then
        log_msg "  ✓ ubus - available for interface status checks"
    else
        log_msg "  ⚠ ubus - not found (interface checks will be limited)"
    fi
    
    # Report missing critical dependencies
    if [ -n "$missing_deps" ]; then
        die 1 "Missing required dependencies:$missing_deps"
    fi
    
    # Warn about remote connectivity tools
    if [ $has_connectivity_tool -eq 0 ]; then
        die 1 "Missing remote connectivity tools. Install at least one of: nc, curl, or wget"
    fi
    
    # Report missing optional dependencies
    if [ -n "$optional_deps" ]; then
        log_msg "  ℹ Optional tools not found:$optional_deps"
        log_msg "  ℹ Remote server checks will use available alternatives"
    fi
    
    log_msg "Dependency check complete - all required tools available"
}

###############################################################################
# PID FILE MANAGEMENT
###############################################################################

check_existing_instance() {
    local retry_count=0
    local max_retries=3
    
    while [ $retry_count -lt $max_retries ]; do
        if [ -f "$PID_FILE" ]; then
            local old_pid
            old_pid="$(cat "$PID_FILE" 2>/dev/null)"
            if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
                if [ $retry_count -eq $((max_retries - 1)) ]; then
                    die 1 "Daemon already running with PID $old_pid"
                fi
                log_msg "Found existing instance (PID $old_pid), waiting..."
                sleep 2
                retry_count=$((retry_count + 1))
            else
                log_msg "Removing stale PID file (PID $old_pid no longer exists)"
                rm -f "$PID_FILE"
                return 0
            fi
        else
            return 0
        fi
    done
}

create_pid_file() {
    if ! echo $$ > "$PID_FILE"; then
        die 1 "Failed to create PID file $PID_FILE"
    fi
}

###############################################################################
# STATUS FILE MANAGEMENT
###############################################################################

update_status() {
    local xray_state="$1"      # 0=connected, 1=disconnected, -1=checking/stopped
    local xray_status="$2"
    local inet_state="${3:-0}"  # 0=connected, 1=disconnected, -1=checking (default: connected)
    local inet_status="${4:-connected}"
    
    mkdir -p "$STATUS_DIR"
    
    cat > "$STATUS_FILE" <<EOF
{
  "instances": [
    {
      "instance": "xray-tunnel",
      "num": "1",
      "inet": $xray_state,
      "status": "$xray_status"
    },
    {
      "instance": "internet",
      "num": "2",
      "inet": $inet_state,
      "status": "$inet_status"
    }
  ]
}
EOF
}

###############################################################################
# STATE MANAGEMENT
###############################################################################

save_state() {
    local mode="$1"           # proxy, direct, checking
    local retry_count="$2"
    local start_time="$3"
    
    cat > "$STATE_FILE" <<EOF
MODE=$mode
RETRY_COUNT=$retry_count
START_TIME=$start_time
EOF
}

load_state() {
    if [ -f "$STATE_FILE" ]; then
        . "$STATE_FILE"
        echo "${MODE:-proxy} ${RETRY_COUNT:-0} ${START_TIME:-$(date +%s)}"
    else
        echo "proxy 0 $(date +%s)"
    fi
}

clear_state() {
    rm -f "$STATE_FILE"
}

cleanup_unclean_shutdown() {
    log_msg "Checking for unclean shutdown state..."
    
    # Check if state file exists from previous run
    if [ ! -f "$STATE_FILE" ]; then
        log_msg "No previous state found - clean start"
        return 0
    fi
    
    # Load previous state
    . "$STATE_FILE"
    local prev_mode="${MODE:-unknown}"
    local prev_retry="${RETRY_COUNT:-0}"
    
    log_msg "Detected previous state: mode=$prev_mode, retry_count=$prev_retry"
    
    # Check current xray status
    local xray_enabled=0
    local service
    service="$(detect_xray_service)"
    if /etc/init.d/$service enabled 2>/dev/null; then
        xray_enabled=1
    fi
    
    local xray_running=0
    if check_xray_running; then
        xray_running=1
    fi
    
    # Analyze state consistency
    if [ "$prev_mode" = "direct" ]; then
        log_msg "Previous run was in failover mode (direct internet)"
        
        if [ $xray_enabled -eq 0 ]; then
            log_msg "Xray is disabled - continuing in direct mode"
            log_msg "Will retry tunnel restoration on next check cycle"
            update_status 1 "direct-mode-resumed"
        else
            log_msg "WARNING: Inconsistent state - xray enabled but state says direct mode"
            log_msg "Attempting to determine current operational mode..."
            
            if [ $xray_running -eq 1 ]; then
                log_msg "Xray is running - testing tunnel connectivity"
                if diagnose_connectivity; then
                    log_msg "Tunnel is working - clearing old failover state"
                    clear_state
                    update_status 0 "tunnel-mode"
                else
                    log_msg "Tunnel not working - forcing failover state"
                    failover_to_direct
                fi
            else
                log_msg "Xray enabled but not running - starting it"
                start_xray
                sleep 5
                if diagnose_connectivity; then
                    log_msg "Tunnel restored - clearing old failover state"
                    clear_state
                    update_status 0 "tunnel-mode"
                else
                    log_msg "Tunnel still not working - forcing failover state"
                    failover_to_direct
                fi
            fi
        fi
    elif [ "$prev_mode" = "proxy" ]; then
        log_msg "Previous run was in normal proxy mode"
        
        if [ $xray_enabled -eq 1 ]; then
            log_msg "Xray is enabled - checking if running"
            
            if [ $xray_running -eq 0 ]; then
                log_msg "Xray not running - starting it"
                start_xray
                sleep 5
            fi
            
            if diagnose_connectivity; then
                log_msg "Tunnel is healthy - resuming normal operation"
                clear_state
                update_status 0 "tunnel-mode"
            else
                log_msg "Tunnel is down - will initiate failover on first check"
                update_status -1 "checking"
            fi
        else
            log_msg "WARNING: Inconsistent state - xray disabled but state says proxy mode"
            log_msg "Attempting to restore xray services..."
            
            enable_xray
            start_xray
            sleep 5
            
            if diagnose_connectivity; then
                log_msg "Tunnel restored successfully"
                clear_state
                update_status 0 "tunnel-mode"
            else
                log_msg "Tunnel not available - will initiate failover on first check"
                update_status -1 "checking"
            fi
        fi
    else
        log_msg "Unknown previous mode - clearing state and starting fresh"
        clear_state
        update_status -1 "unknown"
    fi
    
    log_msg "Unclean shutdown recovery complete"
}

###############################################################################
# XRAY SERVICE MANAGEMENT
###############################################################################

detect_xray_service() {
    if [ -x /etc/init.d/xray_core ]; then
        echo "xray_core"
    elif [ -x /etc/init.d/xray ]; then
        echo "xray"
    else
        die 1 "Cannot find xray init script (/etc/init.d/xray_core or /etc/init.d/xray)"
    fi
}

check_xray_running() {
    # Check if xray process is actually running (most reliable method)
    # PID check is more reliable than init.d status which can be stale
    if pidof xray >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

check_xray_fully_operational() {
    # More thorough check: process running AND ports listening
    if ! check_xray_running; then
        return 1
    fi
    
    # Give xray a moment to initialize ports if just started
    sleep 2
    
    if ! check_xray_ports_listening; then
        log_msg "WARNING: Xray process running but DNS port not ready"
        return 1
    fi
    
    return 0
}

start_xray() {
    local service
    service="$(detect_xray_service)"
    
    log_msg "Starting xray service..."
    /etc/init.d/$service start >/dev/null 2>&1
    sleep 3
    
    if check_xray_running; then
        log_msg "Xray started successfully"
        return 0
    else
        log_msg "WARNING: Xray failed to start"
        return 1
    fi
}

stop_xray() {
    local service
    service="$(detect_xray_service)"
    
    log_msg "Stopping xray service..."
    /etc/init.d/$service stop >/dev/null 2>&1
    killall -q xray 2>/dev/null || true
    rm -rf /var/etc/xray/* 2>/dev/null || true
    
    sleep 2
    
    if check_xray_running; then
        log_msg "WARNING: Xray still running after stop attempt"
        return 1
    else
        log_msg "Xray stopped successfully"
        return 0
    fi
}

enable_xray() {
    local service
    service="$(detect_xray_service)"
    log_msg "Enabling xray service (autostart on boot)..."
    /etc/init.d/$service enable
}

disable_xray() {
    local service
    service="$(detect_xray_service)"
    log_msg "Disabling xray service (no autostart on boot)..."
    /etc/init.d/$service disable
}

validate_xray_config() {
    if [ ! -x /usr/bin/xray ]; then
        log_msg "WARNING: /usr/bin/xray not found, skipping config validation"
        return 0
    fi
    
    log_msg "Validating xray configuration..."
    local output
    output="$(/usr/bin/xray run -test -confdir /var/etc/xray 2>&1 | head -20)"
    
    if echo "$output" | grep -qi "configuration ok\|test passed"; then
        log_msg "Xray configuration valid"
        return 0
    else
        log_msg "ERROR: Xray configuration validation failed:"
        log_msg "$output"
        return 1
    fi
}

###############################################################################
# DNS CONFIGURATION MANAGEMENT
###############################################################################
# NOTE: Xray manages DNS interception through nftables/firewall rules.
# When xray is running, it intercepts DNS queries BEFORE they reach the upstream
# servers listed in /etc/config/dhcp (list server '5.141.95.250')
# Those servers remain configured as fallback for:
#   - Bypassed domains (e.g., xray server FQDN, local networks)
#   - Direct DNS queries when xray is stopped
#   - System DNS resolution (router itself uses 127.0.0.1 -> dnsmasq -> upstream)
# 
# We do NOT modify /etc/config/dhcp - xray's init scripts handle DNS routing
###############################################################################

restart_dns_services() {
    log_msg "Restarting DNS services..."
    
    # Restart dnsmasq to ensure clean state
    /etc/init.d/dnsmasq restart >/dev/null 2>&1
    
    log_msg "DNS services restarted"
}

###############################################################################
# CONNECTIVITY TESTING
###############################################################################

check_upstream_internet() {
    # Check if basic ISP internet is working before blaming xray
    # This prevents false positives when ISP connection is down
    # Uses ISP DNS + Russian public DNS to avoid false positives from blocking
    
    # Test 0: Check WAN interface status via ubus (most reliable)
    if command -v ubus >/dev/null 2>&1; then
        # Check if pppoe-wan interface exists and is up
        if ubus call network.interface.pppoe-wan status >/dev/null 2>&1; then
            local pppoe_status
            pppoe_status="$(ubus call network.interface.pppoe-wan status 2>/dev/null)"
            if echo "$pppoe_status" | grep -q '"up"[[:space:]]*:[[:space:]]*true'; then
                log_msg "Upstream internet check: OK (pppoe-wan interface is UP)"
            else
                log_msg "WARNING: pppoe-wan interface exists but is DOWN - ISP connection issue"
                log_msg "PPPoE status: $(echo "$pppoe_status" | grep -o '"up"[[:space:]]*:[[:space:]]*[^,]*')"
                return 1
            fi
        # Fallback to generic wan interface if pppoe-wan doesn't exist
        elif ubus call network.interface.wan status >/dev/null 2>&1; then
            local wan_status
            wan_status="$(ubus call network.interface.wan status 2>/dev/null)"
            if echo "$wan_status" | grep -q '"up"[[:space:]]*:[[:space:]]*true'; then
                log_msg "Upstream internet check: OK (wan interface is UP)"
            else
                log_msg "WARNING: wan interface is DOWN - ISP connection issue"
                return 1
            fi
        fi
    fi
    
    # Test 1: Ping ISP DNS server
    if ping -c 2 -W 3 "$UPSTREAM_DNS" >/dev/null 2>&1; then
        log_msg "Upstream internet check: OK (ping to ISP DNS $UPSTREAM_DNS successful)"
        return 0
    fi
    
    # Test 2: Ping Yandex DNS (77.88.8.8)
    if ping -c 2 -W 3 "$UPSTREAM_TEST_HOST" >/dev/null 2>&1; then
        log_msg "Upstream internet check: OK (ping to Yandex DNS $UPSTREAM_TEST_HOST successful)"
        return 0
    fi
    
    # Test 3: Ping alternate Yandex DNS (77.88.8.1)
    if ping -c 2 -W 3 "77.88.8.1" >/dev/null 2>&1; then
        log_msg "Upstream internet check: OK (ping to Yandex DNS 77.88.8.1 successful)"
        return 0
    fi
    
    # Test 4: Try DNS query as final fallback
    if nslookup google.com "$UPSTREAM_DNS" >/dev/null 2>&1; then
        log_msg "Upstream internet check: OK (DNS query via $UPSTREAM_DNS successful)"
        return 0
    fi
    
    log_msg "WARNING: Upstream internet appears DOWN (no ISP connectivity)"
    log_msg "Tested: ISP DNS $UPSTREAM_DNS, Yandex DNS 77.88.8.8, 77.88.8.1"
    return 1
}

check_xray_ports_listening() {
    # Validate xray DNS listener is actually working
    # Port 5300 is the primary DNS listener for xray
    
    if command -v ss >/dev/null 2>&1; then
        if ss -lun 2>/dev/null | grep -q '127.0.0.1:5300\|\*:5300'; then
            log_msg "Xray DNS port check: Port 5300 is listening"
            return 0
        fi
    elif command -v netstat >/dev/null 2>&1; then
        if netstat -lun 2>/dev/null | grep -q '127.0.0.1:5300\|0.0.0.0:5300'; then
            log_msg "Xray DNS port check: Port 5300 is listening"
            return 0
        fi
    fi
    
    log_msg "WARNING: Xray DNS port 5300 not listening (xray may not be fully initialized)"
    return 1
}

check_remote_server() {
    # Check if remote xray server is actually reachable
    # This tests if your-xray-server.example.com:443 is responding
    log_msg "Testing remote xray server connectivity: $TUNNEL_FQDN:$TUNNEL_PORT"
    
    # Try nc (netcat) first - most reliable for simple port check
    if command -v nc >/dev/null 2>&1; then
        if timeout 5 nc -z "$TUNNEL_FQDN" "$TUNNEL_PORT" 2>/dev/null; then
            log_msg "Remote server check: OK (server is reachable)"
            return 0
        fi
    fi
    
    # Fallback to curl with SSL check
    if command -v curl >/dev/null 2>&1; then
        # Try to connect to the server, expecting TLS handshake
        # Even if it's not HTTP, curl will at least verify TCP connection
        if curl --connect-timeout 5 --max-time 10 -k -s "https://$TUNNEL_FQDN:$TUNNEL_PORT" >/dev/null 2>&1; then
            log_msg "Remote server check: OK (server is reachable via curl)"
            return 0
        fi
    fi
    
    # Fallback to wget if curl not available
    if command -v wget >/dev/null 2>&1; then
        if timeout 10 wget --timeout=5 --tries=1 --no-check-certificate -q -O /dev/null "https://$TUNNEL_FQDN:$TUNNEL_PORT" 2>/dev/null; then
            log_msg "Remote server check: OK (server is reachable via wget)"
            return 0
        fi
    fi
    
    log_msg "ERROR: Remote xray server $TUNNEL_FQDN:$TUNNEL_PORT is not reachable"
    log_msg "Server may be down, blocked, or network path is broken"
    return 1
}

test_tunnel_connectivity() {
    # Test if we can reach external sites THROUGH the tunnel
    # We test google.com which should be proxied through xray
    # If xray is working, this will succeed. If not, it will fail or timeout.
    local test_url="https://www.google.com"
    
    # Test from LAN interface perspective
    # Timeout is critical - if tunnel is down, this will hang
    local http_code
    http_code="$(curl --interface "$LAN_IFACE" \
         --connect-timeout 10 \
         --max-time 15 \
         -s -o /dev/null \
         -w "%{http_code}" \
         "$test_url" 2>/dev/null)"
    
    echo "$http_code"
}

diagnose_connectivity() {
    update_status -1 "checking"
    
    # STEP 1: Check if upstream ISP internet is working
    # No point checking xray if there's no internet at all
    if ! check_upstream_internet; then
        log_msg "Cannot diagnose xray - no upstream ISP connectivity"
        log_msg "This may be a PPPoE/WAN issue, not xray issue"
        # Don't fail over in this case - wait for ISP to recover
        return 2  # Special code: ISP down, not xray's fault
    fi
    
    # STEP 2: Check if xray is running and ports are listening
    if ! check_xray_running; then
        log_msg "Xray is not running, attempting to start..."
        if start_xray; then
            sleep 5  # Give it time to initialize
            if ! check_xray_fully_operational; then
                log_msg "Xray started but not fully operational"
                return 1
            fi
        else
            return 1  # Failed to start
        fi
    else
        # Xray is running, verify ports are listening
        if ! check_xray_ports_listening; then
            log_msg "WARNING: Xray running but ports not listening - may be initializing"
            sleep 3
            if ! check_xray_ports_listening; then
                log_msg "ERROR: Xray ports still not listening after wait"
                return 1
            fi
        fi
    fi
    
    # STEP 3: Check if remote xray server is reachable
    if ! check_remote_server; then
        log_msg "ERROR: Remote xray server is not reachable"
        return 1
    fi
    
    # All checks passed - tunnel is operational
    log_msg "Tunnel validation: Local process + ports + remote server = operational"
    log_msg "Tunnel is healthy and operational"
    return 0
}

###############################################################################
# FAILOVER LOGIC
###############################################################################

failover_to_direct() {
    log_msg "==================== FAILOVER TO DIRECT INTERNET ===================="
    update_status 1 "failing-over" 0 "connected"
    
    # Stop and disable xray
    stop_xray
    disable_xray
    
    # Restart firewall to clear xray nftables rules
    # This removes DNS interception - dnsmasq will use upstream servers directly
    log_msg "Restarting firewall to remove xray rules..."
    /etc/init.d/firewall restart >/dev/null 2>&1
    
    # Restart DNS services to ensure clean state
    restart_dns_services
    
    update_status 1 "direct-mode" 0 "connected"
    log_msg "Failover complete - operating in direct internet mode"
    log_msg "DNS now routes directly to upstream servers (5.141.95.250, 5.141.95.254)"
}

restore_tunnel() {
    log_msg "==================== RESTORING TUNNEL MODE ===================="
    update_status -1 "restoring"
    
    # Enable and start xray
    enable_xray
    if ! start_xray; then
        log_msg "ERROR: Failed to start xray during restore"
        update_status 1 "restore-failed"
        return 1
    fi
    
    # Validate configuration
    sleep 5  # Wait for config generation
    if ! validate_xray_config; then
        log_msg "ERROR: Xray config validation failed during restore"
        update_status 1 "config-invalid"
        return 1
    fi
    
    # Restart firewall to apply xray nftables rules (DNS interception)
    log_msg "Restarting firewall to apply xray rules..."
    /etc/init.d/firewall restart >/dev/null 2>&1
    
    # Restart DNS services to ensure xray DNS interception is active
    restart_dns_services
    
    # Wait for services to stabilize
    sleep 10
    
    # Verify tunnel connectivity
    local diag_result=0
    diagnose_connectivity
    diag_result=$?
    
    if [ $diag_result -eq 0 ]; then
        update_status 0 "tunnel-mode" 0 "connected"
        log_msg "Tunnel restored successfully"
        log_msg "DNS now routes through xray's encrypted DoH servers"
        clear_state
        return 0
    elif [ $diag_result -eq 2 ]; then
        log_msg "WARNING: Upstream ISP still down, but xray restored"
        update_status -1 "waiting-for-isp" 1 "disconnected"
        return 1
    else
        log_msg "ERROR: Tunnel still unreachable after restore"
        update_status 1 "tunnel-unreachable" 0 "connected"
        return 1
    fi
}

###############################################################################
# RESTORE COMMAND
###############################################################################

restore_command() {
    local force="$1"
    
    if [ "$force" = "--force" ]; then
        log_msg "==================== FORCED MANUAL RESTORE (NO CONNECTIVITY CHECK) ===================="
    else
        log_msg "==================== MANUAL RESTORE REQUESTED ===================="
    fi
    
    # Remove PID file to prevent conflicts with daemon mode
    rm -f "$PID_FILE"
    
    # Ensure status directory exists
    mkdir -p "$STATUS_DIR"
    
    # Set initial status for LuCI widget
    update_status -1 "manual-restore" 0 "connected"
    
    if [ "$force" = "--force" ]; then
        # Force restore without connectivity check
        log_msg "Enabling and starting xray services..."
        enable_xray
        if ! start_xray; then
            log_msg "ERROR: Failed to start xray during forced restore"
            update_status 1 "restore-failed" 0 "connected"
            exit 1
        fi
        
        sleep 5
        
        log_msg "Restarting firewall to apply xray rules..."
        /etc/init.d/firewall restart
        
        restart_dns_services
        
        log_msg "Forced restore complete (tunnel enabled, connectivity not verified)"
        update_status 0 "tunnel-mode" 0 "connected"
        clear_state
        exit 0
    else
        # Normal restore with connectivity check
        if restore_tunnel; then
            log_msg "Manual restore completed successfully"
            exit 0
        else
            log_msg "Manual restore failed - xray may need manual intervention"
            log_msg "Use --force-restore to restore without connectivity check"
            update_status 1 "manual-restore-failed" 0 "connected"
            exit 1
        fi
    fi
}

###############################################################################
# MAIN DAEMON LOOP
###############################################################################

daemon_loop() {
    log_msg "==================== XRAY HEALTH DAEMON STARTED ===================="
    log_msg "Configuration: $TUNNEL_FQDN ($TUNNEL_IP:$TUNNEL_PORT)"
    log_msg "LAN Interface: $LAN_IFACE"
    log_msg "PID: $$"
    
    # Load previous state if exists
    local state_output
    state_output="$(load_state)"
    read -r current_mode retry_count outage_start <<EOF
$state_output
EOF
    
    # Initialize last log rotation check
    local last_log_check
    last_log_check="$(date +%s)"
    
    while true; do
        # Check if log rotation is needed (check once per day)
        local current_time
        current_time="$(date +%s)"
        if [ $((current_time - last_log_check)) -ge 86400 ]; then
            rotate_logs
            last_log_check=$current_time
        fi
        
        local diag_result=0
        diagnose_connectivity
        diag_result=$?
        
        if [ $diag_result -eq 0 ]; then
            # Tunnel is healthy
            if [ "$current_mode" = "direct" ]; then
                # We were in failover mode, try to restore
                log_msg "Tunnel is back online, attempting to restore..."
                if restore_tunnel; then
                    current_mode="proxy"
                    retry_count=0
                    save_state "$current_mode" 0 0
                else
                    log_msg "Restore failed, will retry later"
                fi
            else
                # Normal operation
                update_status 0 "tunnel-mode" 0 "connected"
                if [ "$current_mode" != "proxy" ]; then
                    log_msg "Tunnel healthy - normal operation"
                    current_mode="proxy"
                    retry_count=0
                    save_state "$current_mode" 0 0
                fi
            fi
            sleep $CHECK_INTERVAL_NORMAL
        elif [ $diag_result -eq 2 ]; then
            # ISP internet is down - don't change mode, just wait
            log_msg "ISP internet down - waiting for recovery (not xray issue)"
            # Show error state (red) so user knows there's an issue, not checking state (grey spinner)
            update_status 1 "isp-down" 1 "isp-down"
            log_msg "Sleeping for $CHECK_INTERVAL_SHORT seconds before next ISP check..."
            sleep $CHECK_INTERVAL_SHORT
            log_msg "Woke from sleep, will recheck ISP connectivity"
        else
            # Tunnel is down - update status immediately to show error
            update_status 1 "tunnel-down" 0 "connected"
            
            if [ "$current_mode" = "proxy" ]; then
                # First failure detected
                log_msg "Tunnel failure detected, initiating failover..."
                failover_to_direct
                current_mode="direct"
                retry_count=0
                outage_start=$(date +%s)
                save_state "$current_mode" "$retry_count" "$outage_start"
            fi
            
            # Calculate elapsed time and appropriate retry interval
            local current_time
            current_time="$(date +%s)"
            local elapsed=$((current_time - outage_start))
            retry_count=$((retry_count + 1))
            
            if [ $elapsed -gt $MAX_RETRY_DURATION ]; then
                log_msg "Max retry duration exceeded, giving up"
                log_msg "Xray tunnel permanently disabled - manual intervention required"
                update_status 1 "permanent-failure"
                save_state "$current_mode" "$retry_count" "$outage_start"
                exit 0
            fi
            
            # Determine sleep interval
            local sleep_time=$CHECK_INTERVAL_SHORT
            if [ $elapsed -gt $SHORT_INTERVAL_DURATION ]; then
                sleep_time=$CHECK_INTERVAL_LONG
            fi
            
            log_msg "Retry attempt $retry_count - will check again after sleep"
            
            save_state "$current_mode" "$retry_count" "$outage_start"
            sleep $sleep_time
        fi
    done
}

###############################################################################
# MAIN ENTRY POINT
###############################################################################

main() {
    # Handle restore parameters
    if [ "$1" = "--restore" ] || [ "$1" = "restore" ]; then
        restore_command
        exit $?
    elif [ "$1" = "--force-restore" ] || [ "$1" = "force-restore" ]; then
        restore_command --force
        exit $?
    fi
    
    # Check for existing instance
    check_existing_instance
    
    # Check dependencies before starting
    check_dependencies
    
    # Create PID file
    create_pid_file
    
    # Initialize status
    mkdir -p "$STATUS_DIR"
    update_status -1 "starting" -1 "checking"
    
    # Check for and recover from unclean shutdown
    cleanup_unclean_shutdown
    
    # Start daemon loop
    daemon_loop
}

# Run main function
main "$@"
