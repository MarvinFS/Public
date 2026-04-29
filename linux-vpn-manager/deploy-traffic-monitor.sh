#!/bin/bash
# deploy-traffic-monitor.sh - Automated deployment of VPN traffic monitoring stack
# Installs pmacct + SQLite + web dashboard + CLI tool on a fresh Ubuntu 24.04 server
#
# Usage:
#   ./deploy-traffic-monitor.sh [options]
#
# The script auto-detects interfaces and IPs where possible, and prompts
# for anything it cannot determine automatically.

set -euo pipefail

# ---------- defaults (overridable via environment) ----------
DASHBOARD_PORT="${DASHBOARD_PORT:-3000}"
RETENTION_DAYS="${RETENTION_DAYS:-180}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------- colors ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[x]${NC} $*" >&2; }
ask()  { echo -en "${CYAN}[?]${NC} $1: "; }

# ---------- root check ----------
if [ "$(id -u)" -ne 0 ]; then
    err "This script must be run as root."
    exit 1
fi

# ---------- OS check ----------
if ! grep -q 'Ubuntu' /etc/os-release 2>/dev/null; then
    warn "This script is designed for Ubuntu 24.04. Other distros may work but are untested."
fi

# ---------- detect interfaces and IPs ----------
detect_config() {
    log "Detecting network configuration..."

    # WireGuard interface
    WG_IFACE=""
    for iface in $(ip -o link show | awk -F': ' '{print $2}'); do
        if ip link show "$iface" 2>/dev/null | grep -q 'POINTOPOINT'; then
            if ip addr show "$iface" 2>/dev/null | grep -q 'inet '; then
                WG_IFACE="$iface"
                break
            fi
        fi
    done

    if [ -n "$WG_IFACE" ]; then
        WG_SERVER_IP=$(ip -4 addr show "$WG_IFACE" | grep -oP 'inet \K[0-9.]+')
        WG_SUBNET=$(ip -4 addr show "$WG_IFACE" | grep -oP 'inet \K[0-9./]+' | sed 's|/.*||')
        WG_SUBNET="${WG_SUBNET%.*}.0/24"
        log "Found WireGuard: $WG_IFACE ($WG_SERVER_IP, subnet $WG_SUBNET)"
    else
        warn "No WireGuard interface detected."
    fi

    # Main network interface (first non-lo, non-wg, non-docker interface with a default route)
    MAIN_IFACE=$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)
    if [ -z "$MAIN_IFACE" ]; then
        MAIN_IFACE=$(ip -o -4 addr show scope global | awk '{print $2}' | grep -v "^${WG_IFACE}$" | head -1)
    fi
    MAIN_IP=$(ip -4 addr show "$MAIN_IFACE" 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1)
    log "Main interface: $MAIN_IFACE ($MAIN_IP)"

    # Xray port detection
    XRAY_PORT=""
    if command -v xray >/dev/null 2>&1; then
        XRAY_PID=$(pgrep -x xray 2>/dev/null | head -1 || true)
        if [ -n "$XRAY_PID" ]; then
            XRAY_PORT=$(ss -tlnp 2>/dev/null | grep "pid=${XRAY_PID}" | awk '{print $4}' | grep -oP ':\K[0-9]+' | head -1)
        fi
    fi
    if [ -z "$XRAY_PORT" ]; then
        # try common xray/reality ports
        for port in 443 8443 48721; do
            if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
                XRAY_PORT="$port"
                break
            fi
        done
    fi
    if [ -n "$XRAY_PORT" ]; then
        log "Detected Xray port: $XRAY_PORT"
    else
        warn "No Xray port detected."
    fi
}

# ---------- interactive confirmation ----------
confirm_config() {
    echo ""
    echo "========================================="
    echo "  VPN Traffic Monitor Configuration"
    echo "========================================="
    echo ""

    if [ -n "$MAIN_IFACE" ]; then
        ask "Main interface [$MAIN_IFACE]"
        read -r input; [ -n "$input" ] && MAIN_IFACE="$input"
    else
        ask "Main interface (e.g., eth0, ens18)"
        read -r MAIN_IFACE
    fi

    MAIN_IP=$(ip -4 addr show "$MAIN_IFACE" 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1)
    ask "Server IP [$MAIN_IP]"
    read -r input; [ -n "$input" ] && MAIN_IP="$input"

    # WireGuard
    ENABLE_WG="n"
    if [ -n "$WG_IFACE" ]; then
        ask "Enable WireGuard monitoring? [Y/n]"
        read -r input; ENABLE_WG="${input:-y}"
    else
        ask "Enable WireGuard monitoring? [y/N]"
        read -r input; ENABLE_WG="${input:-n}"
    fi
    ENABLE_WG=$(echo "$ENABLE_WG" | tr '[:upper:]' '[:lower:]')

    if [ "$ENABLE_WG" = "y" ]; then
        ask "WireGuard interface [$WG_IFACE]"
        read -r input; [ -n "$input" ] && WG_IFACE="$input"

        WG_SERVER_IP=$(ip -4 addr show "$WG_IFACE" 2>/dev/null | grep -oP 'inet \K[0-9.]+' | head -1)
        ask "WireGuard server IP [$WG_SERVER_IP]"
        read -r input; [ -n "$input" ] && WG_SERVER_IP="$input"
    fi

    # Xray
    ENABLE_XRAY="n"
    if [ -n "$XRAY_PORT" ]; then
        ask "Enable Xray monitoring? [Y/n]"
        read -r input; ENABLE_XRAY="${input:-y}"
    else
        ask "Enable Xray monitoring? [y/N]"
        read -r input; ENABLE_XRAY="${input:-n}"
    fi
    ENABLE_XRAY=$(echo "$ENABLE_XRAY" | tr '[:upper:]' '[:lower:]')

    if [ "$ENABLE_XRAY" = "y" ]; then
        ask "Xray port [$XRAY_PORT]"
        read -r input; [ -n "$input" ] && XRAY_PORT="$input"

        # Allow monitoring multiple ports (e.g. REALITY on 48721 + XHTTP behind
        # nginx on 443). Separate with spaces. Each port produces a pair of
        # (src port N and src host MAIN_IP) / (dst port N and dst host MAIN_IP)
        # BPF clauses so xray's upstream connections are excluded.
        XRAY_PORTS="$XRAY_PORT"
        ask "Additional Xray/nginx ingress ports (space-separated, e.g. 443) [none]"
        read -r input
        if [ -n "$input" ]; then
            XRAY_PORTS="$XRAY_PORT $input"
        fi
    fi

    ask "Dashboard port [$DASHBOARD_PORT]"
    read -r input; [ -n "$input" ] && DASHBOARD_PORT="$input"

    ask "Data retention in days [$RETENTION_DAYS]"
    read -r input; [ -n "$input" ] && RETENTION_DAYS="$input"

    if [ "$ENABLE_WG" != "y" ] && [ "$ENABLE_XRAY" != "y" ]; then
        err "At least one VPN protocol must be enabled."
        exit 1
    fi

    echo ""
    log "Configuration summary:"
    echo "  Main interface: $MAIN_IFACE ($MAIN_IP)"
    [ "$ENABLE_WG" = "y" ] && echo "  WireGuard:      $WG_IFACE ($WG_SERVER_IP)"
    [ "$ENABLE_XRAY" = "y" ] && echo "  Xray:           ports ${XRAY_PORTS:-$XRAY_PORT} on $MAIN_IFACE"
    echo "  Dashboard:      port $DASHBOARD_PORT"
    echo "  Retention:      $RETENTION_DAYS days"
    echo ""
    ask "Proceed with installation? [Y/n]"
    read -r input
    if [ "$(echo "${input:-y}" | tr '[:upper:]' '[:lower:]')" != "y" ]; then
        log "Aborted."
        exit 0
    fi
}

# ---------- install packages ----------
install_packages() {
    log "Installing packages..."
    apt-get update -qq
    apt-get install -y -qq pmacct sqlite3 bc python3-maxminddb >/dev/null 2>&1
    log "Packages installed."
}

# ---------- create patched SQLite databases ----------
create_databases() {
    log "Creating SQLite databases with Ubuntu 24.04 bugfix schema..."
    mkdir -p /var/lib/pmacct

    local SCHEMA="CREATE TABLE IF NOT EXISTS acct (
        mac_src CHAR(17) NOT NULL DEFAULT '0:0:0:0:0:0',
        mac_dst CHAR(17) NOT NULL DEFAULT '0:0:0:0:0:0',
        ip_src CHAR(45) NOT NULL DEFAULT '0.0.0.0',
        ip_dst CHAR(45) NOT NULL DEFAULT '0.0.0.0',
        src_port INT(4) NOT NULL DEFAULT 0,
        dst_port INT(4) NOT NULL DEFAULT 0,
        ip_proto CHAR(6) NOT NULL DEFAULT 0,
        vlan_in INT(4) NOT NULL DEFAULT 0,
        vlan_out INT(4) NOT NULL DEFAULT 0,
        packets INT NOT NULL,
        bytes BIGINT NOT NULL,
        stamp_inserted DATETIME NOT NULL DEFAULT '0000-00-00 00:00:00',
        stamp_updated DATETIME,
        PRIMARY KEY (mac_src, mac_dst, ip_src, ip_dst, src_port, dst_port,
                     ip_proto, vlan_in, vlan_out, stamp_inserted)
    );"

    if [ "$ENABLE_XRAY" = "y" ]; then
        if [ ! -f /var/lib/pmacct/xray.db ] || [ ! -s /var/lib/pmacct/xray.db ]; then
            rm -f /var/lib/pmacct/xray.db
            sqlite3 /var/lib/pmacct/xray.db "$SCHEMA"
            log "Created xray.db"
        else
            warn "xray.db already exists and has data, skipping."
        fi
    fi

    if [ "$ENABLE_WG" = "y" ]; then
        if [ ! -f /var/lib/pmacct/wg.db ] || [ ! -s /var/lib/pmacct/wg.db ]; then
            rm -f /var/lib/pmacct/wg.db
            sqlite3 /var/lib/pmacct/wg.db "$SCHEMA"
            log "Created wg.db"
        else
            warn "wg.db already exists and has data, skipping."
        fi
    fi
}

# ---------- configure pmacct ----------
configure_pmacct() {
    log "Writing pmacct configuration..."
    mkdir -p /etc/pmacct

    if [ "$ENABLE_XRAY" = "y" ]; then
        # Build a precise pcap filter so we only count client<->server traffic on
        # the listener ports (not xray's upstream connections to the internet,
        # which would double-count and add noise).
        #
        # XRAY_PORTS is a space-separated list of TCP ports where the server
        # accepts VPN connections (e.g. "48721 443" for REALITY + XHTTP/nginx).
        local filter=""
        for p in ${XRAY_PORTS:-$XRAY_PORT}; do
            local clause="(dst port ${p} and dst host ${MAIN_IP}) or (src port ${p} and src host ${MAIN_IP})"
            if [ -z "$filter" ]; then
                filter="$clause"
            else
                filter="$filter or $clause"
            fi
        done

        cat > /etc/pmacct/pmacctd-xray.conf << EOFCFG
daemonize: true
pidfile: /run/pmacctd-xray.pid
pcap_interface: ${MAIN_IFACE}
pcap_filter: ${filter}

# Large plugin pipe avoids silent "Missing data" drops under bursty load.
# The default 4MB pipe + 320-byte buffer is too tight for multi-client VPN
# traffic and leads to the plugin pipe eventually stalling.
plugin_buffer_size: 10240
plugin_pipe_size: 104857600

plugins: memory[live], sqlite3[hist]

aggregate[live]: src_host, dst_host
imt_path[live]: /tmp/pmacct-xray.pipe
imt_mem_pools_number[live]: 256

aggregate[hist]: src_host, dst_host
sql_db[hist]: /var/lib/pmacct/xray.db
sql_table_version[hist]: 1
sql_refresh_time[hist]: 60
sql_history[hist]: 1h
sql_history_roundoff[hist]: h
EOFCFG
        log "Wrote /etc/pmacct/pmacctd-xray.conf"
    fi

    if [ "$ENABLE_WG" = "y" ]; then
        cat > /etc/pmacct/pmacctd-wg.conf << EOFCFG
daemonize: true
pidfile: /run/pmacctd-wg.pid
pcap_interface: ${WG_IFACE}

plugin_buffer_size: 10240
plugin_pipe_size: 104857600

plugins: memory[live], sqlite3[hist]

aggregate[live]: src_host, dst_host
imt_path[live]: /tmp/pmacct-wg.pipe
imt_mem_pools_number[live]: 256

aggregate[hist]: src_host, dst_host
sql_db[hist]: /var/lib/pmacct/wg.db
sql_table_version[hist]: 1
sql_refresh_time[hist]: 60
sql_history[hist]: 1h
sql_history_roundoff[hist]: h
EOFCFG
        log "Wrote /etc/pmacct/pmacctd-wg.conf"
    fi
}

# ---------- create systemd services for pmacct ----------
create_pmacct_services() {
    log "Creating pmacct systemd services..."

    if [ "$ENABLE_XRAY" = "y" ]; then
        cat > /etc/systemd/system/pmacctd-xray.service << 'EOF'
[Unit]
Description=pmacctd Xray traffic accounting
After=network.target

[Service]
Type=forking
PIDFile=/run/pmacctd-xray.pid
ExecStart=/usr/sbin/pmacctd -f /etc/pmacct/pmacctd-xray.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    fi

    if [ "$ENABLE_WG" = "y" ]; then
        cat > /etc/systemd/system/pmacctd-wg.service << 'EOF'
[Unit]
Description=pmacctd WireGuard traffic accounting
After=network.target

[Service]
Type=forking
PIDFile=/run/pmacctd-wg.pid
ExecStart=/usr/sbin/pmacctd -f /etc/pmacct/pmacctd-wg.conf
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    fi

    systemctl daemon-reload

    if [ "$ENABLE_XRAY" = "y" ]; then
        systemctl enable --now pmacctd-xray
        log "Started pmacctd-xray"
    fi
    if [ "$ENABLE_WG" = "y" ]; then
        systemctl enable --now pmacctd-wg
        log "Started pmacctd-wg"
    fi
}

# ---------- install GeoIP databases ----------
install_geoip() {
    log "Installing GeoIP databases..."
    mkdir -p /var/lib/GeoIP
    local MONTH
    MONTH=$(date +%Y-%m)

    if [ -f /var/lib/GeoIP/dbip-city-lite.mmdb ] && [ -s /var/lib/GeoIP/dbip-city-lite.mmdb ]; then
        warn "GeoIP databases already exist, skipping download."
        return
    fi

    curl -sL "https://download.db-ip.com/free/dbip-city-lite-${MONTH}.mmdb.gz" | gunzip > /var/lib/GeoIP/dbip-city-lite.mmdb 2>/dev/null
    curl -sL "https://download.db-ip.com/free/dbip-country-lite-${MONTH}.mmdb.gz" | gunzip > /var/lib/GeoIP/dbip-country-lite.mmdb 2>/dev/null

    if [ -s /var/lib/GeoIP/dbip-city-lite.mmdb ]; then
        log "GeoIP databases downloaded ($(du -sh /var/lib/GeoIP/dbip-city-lite.mmdb | awk '{print $1}'))"
    else
        warn "GeoIP download failed. Dashboard will work without location data."
        rm -f /var/lib/GeoIP/dbip-city-lite.mmdb /var/lib/GeoIP/dbip-country-lite.mmdb
    fi

    # Monthly auto-update cron
    cat > /etc/cron.d/geoip-update << 'GEOCRON'
# Update DB-IP GeoIP databases on the 2nd of each month
0 3 2 * * root MONTH=$(date +\%Y-\%m); cd /var/lib/GeoIP && curl -sL "https://download.db-ip.com/free/dbip-city-lite-${MONTH}.mmdb.gz" | gunzip > dbip-city-lite.mmdb.tmp && mv dbip-city-lite.mmdb.tmp dbip-city-lite.mmdb && systemctl restart vpn-dashboard 2>/dev/null
GEOCRON
    log "GeoIP monthly update cron installed."
}

# ---------- install CLI tool ----------
install_cli() {
    log "Installing vpn-stats CLI tool..."

    # Build the case blocks dynamically based on enabled protocols
    local LIVE_BLOCK=""
    local HIST_BLOCK=""

    if [ "$ENABLE_XRAY" = "y" ]; then
        LIVE_BLOCK="${LIVE_BLOCK}        query_live /tmp/pmacct-xray.pipe \"Xray Clients\" \"${MAIN_IP}\"
"
        HIST_BLOCK="${HIST_BLOCK}        query_hist /var/lib/pmacct/xray.db \"Xray Clients\" \"${MAIN_IP}\" \"\$period\"
"
    fi
    if [ "$ENABLE_WG" = "y" ]; then
        LIVE_BLOCK="${LIVE_BLOCK}        query_live /tmp/pmacct-wg.pipe \"WireGuard Peers\" \"${WG_SERVER_IP}\"
"
        HIST_BLOCK="${HIST_BLOCK}        query_hist /var/lib/pmacct/wg.db \"WireGuard Peers\" \"${WG_SERVER_IP}\" \"\$period\"
"
    fi

    cat > /usr/local/bin/vpn-stats << 'VPNSTATS_HEADER'
#!/bin/bash
fmt_bytes() {
    local b=$1
    if [ "$b" -ge 1073741824 ] 2>/dev/null; then
        printf "%.2f GB" $(echo "scale=2; $b/1073741824" | bc)
    elif [ "$b" -ge 1048576 ] 2>/dev/null; then
        printf "%.1f MB" $(echo "scale=1; $b/1048576" | bc)
    elif [ "$b" -ge 1024 ] 2>/dev/null; then
        printf "%.0f KB" $(echo "scale=0; $b/1024" | bc)
    else
        printf "%d B" "$b"
    fi
}
print_header() {
    printf "%-42s %12s %12s %12s\n" "Host" "In" "Out" "Total"
    printf "%-42s %12s %12s %12s\n" \
        "------------------------------------------" "------------" "------------" "------------"
}
query_live() {
    local pipe=$1 label=$2 server_ip=$3
    echo ""; echo "=== $label (Live) ==="; print_header
    pmacct -s -p "$pipe" -O csv 2>/dev/null | tail -n +2 | \
    while IFS="," read -r src dst pkts bytes; do
        src=$(echo "$src" | xargs); dst=$(echo "$dst" | xargs); bytes=$(echo "$bytes" | xargs)
        if [ "$src" = "$server_ip" ]; then echo "OUT $dst $bytes"; else echo "IN $src $bytes"; fi
    done | awk '{
        ip=$2; b=$3; if ($1=="IN") ins[ip]+=b; else outs[ip]+=b; seen[ip]=1
    } END {
        for (ip in seen) { i=ins[ip]+0; o=outs[ip]+0; t=i+o; printf "%s %d %d %d\n", ip, i, o, t }
    }' | sort -t" " -k4 -rn | while read -r ip i o t; do
        printf "%-42s %12s %12s %12s\n" "$ip" "$(fmt_bytes "$i")" "$(fmt_bytes "$o")" "$(fmt_bytes "$t")"
    done
}
query_hist() {
    local db=$1 label=$2 server_ip=$3 period="${4:-today}" where=""
    case "$period" in
        today)     where="stamp_inserted >= date('now', 'start of day')" ;;
        yesterday) where="stamp_inserted >= date('now', '-1 day', 'start of day') AND stamp_inserted < date('now', 'start of day')" ;;
        week)      where="stamp_inserted >= date('now', '-7 days')" ;;
        month)     where="stamp_inserted >= date('now', 'start of month')" ;;
        lastmonth) where="stamp_inserted >= date('now', 'start of month', '-1 month') AND stamp_inserted < date('now', 'start of month')" ;;
        all)       where="1=1" ;;
        *)         where="stamp_inserted >= date('now', '-$period days')" ;;
    esac
    echo ""; echo "=== $label ($period) ==="; print_header
    sqlite3 -separator "|" "$db" "
        SELECT CASE WHEN ip_src = '$server_ip' THEN ip_dst ELSE ip_src END as client,
            SUM(CASE WHEN ip_src != '$server_ip' THEN bytes ELSE 0 END),
            SUM(CASE WHEN ip_src = '$server_ip' THEN bytes ELSE 0 END),
            SUM(bytes) FROM acct WHERE $where GROUP BY client ORDER BY SUM(bytes) DESC;
    " 2>/dev/null | while IFS="|" read -r ip i o t; do
        [ -z "$ip" ] && continue
        printf "%-42s %12s %12s %12s\n" "$ip" "$(fmt_bytes "$i")" "$(fmt_bytes "$o")" "$(fmt_bytes "$t")"
    done
}
VPNSTATS_HEADER

    # Append the case statement with configured values
    cat >> /usr/local/bin/vpn-stats << VPNSTATS_CASE
case "\${1:-live}" in
    live)
${LIVE_BLOCK}        ;;
    today|yesterday|week|month|lastmonth|all|[0-9]*)
        period=\${1:-today}
${HIST_BLOCK}        ;;
    help|*)
        echo "Usage: vpn-stats [live|today|yesterday|week|month|lastmonth|all|N]"
        echo "  live       - current session (default)"
        echo "  today      - today so far"
        echo "  yesterday  - yesterday only"
        echo "  week       - last 7 days"
        echo "  month      - this month"
        echo "  lastmonth  - previous month"
        echo "  all        - all time"
        echo "  N          - last N days"
        ;;
esac
VPNSTATS_CASE

    chmod +x /usr/local/bin/vpn-stats
    log "Installed /usr/local/bin/vpn-stats"
}

# ---------- install web dashboard ----------
install_dashboard() {
    log "Installing web dashboard..."
    mkdir -p /opt/vpn-dashboard

    # Copy dashboard from the same directory as this script, or download
    if [ -f "${SCRIPT_DIR}/vpn-dashboard.py" ]; then
        cp "${SCRIPT_DIR}/vpn-dashboard.py" /opt/vpn-dashboard/vpn-dashboard.py
    else
        err "vpn-dashboard.py not found in ${SCRIPT_DIR}."
        err "Place vpn-dashboard.py next to this script and re-run."
        exit 1
    fi

    # Patch configuration constants in the dashboard
    sed -i "s|^PORT = .*|PORT = ${DASHBOARD_PORT}|" /opt/vpn-dashboard/vpn-dashboard.py
    sed -i "s|^SERVER_IP_XRAY = .*|SERVER_IP_XRAY = \"${MAIN_IP}\"|" /opt/vpn-dashboard/vpn-dashboard.py
    sed -i "s|^RETENTION_DAYS = .*|RETENTION_DAYS = ${RETENTION_DAYS}|" /opt/vpn-dashboard/vpn-dashboard.py

    if [ "$ENABLE_WG" = "y" ]; then
        sed -i "s|^SERVER_IP_WG = .*|SERVER_IP_WG = \"${WG_SERVER_IP}\"|" /opt/vpn-dashboard/vpn-dashboard.py
    fi

    # Create systemd service
    cat > /etc/systemd/system/vpn-dashboard.service << EOF
[Unit]
Description=VPN Traffic Dashboard
After=network.target pmacctd-wg.service pmacctd-xray.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/vpn-dashboard/vpn-dashboard.py
Restart=on-failure
RestartSec=5
WorkingDirectory=/opt/vpn-dashboard

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now vpn-dashboard
    log "Dashboard started on port ${DASHBOARD_PORT}"
}

# ---------- verify ----------
verify() {
    echo ""
    log "Verifying installation..."
    local ok=true

    if [ "$ENABLE_XRAY" = "y" ]; then
        if systemctl is-active --quiet pmacctd-xray; then
            log "pmacctd-xray: running"
        else
            err "pmacctd-xray: not running"
            ok=false
        fi
    fi

    if [ "$ENABLE_WG" = "y" ]; then
        if systemctl is-active --quiet pmacctd-wg; then
            log "pmacctd-wg: running"
        else
            err "pmacctd-wg: not running"
            ok=false
        fi
    fi

    if systemctl is-active --quiet vpn-dashboard; then
        log "vpn-dashboard: running"
    else
        err "vpn-dashboard: not running"
        ok=false
    fi

    # Check dashboard responds
    if command -v curl >/dev/null 2>&1; then
        local code
        code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${DASHBOARD_PORT}/" 2>/dev/null || echo "000")
        if [ "$code" = "200" ]; then
            log "Dashboard HTTP: OK"
        else
            err "Dashboard HTTP: failed (code: $code)"
            ok=false
        fi
    fi

    echo ""
    if [ "$ok" = true ]; then
        log "Installation complete!"
        echo ""
        echo "  Dashboard:  http://${MAIN_IP}:${DASHBOARD_PORT}"
        echo "  CLI:        vpn-stats [live|today|week|month|all]"
        echo ""
        echo "  SQLite data will start appearing after ~60 seconds."
        echo "  Data retention: ${RETENTION_DAYS} days."
    else
        warn "Installation completed with errors. Check the services above."
    fi
}

# ---------- main ----------
main() {
    echo ""
    echo "  VPN Traffic Monitor - Automated Deployment"
    echo "  pmacct + SQLite + Web Dashboard + CLI"
    echo ""

    detect_config
    confirm_config
    install_packages
    create_databases
    configure_pmacct
    create_pmacct_services
    install_geoip
    install_cli
    install_dashboard
    verify
}

main "$@"
