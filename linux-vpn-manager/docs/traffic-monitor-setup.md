# VPN Traffic Monitor Setup

Per-client traffic monitoring for WireGuard and Xray VPN servers running on Ubuntu 24.04. Uses pmacct for packet accounting with SQLite persistence, a Python web dashboard for visualization, and a CLI tool for quick terminal queries.

The stack provides real-time per-host bandwidth rates, accumulated traffic totals (in/out/total per client), historical data with time-range filtering (today, week, month, etc.), and automatic 6-month data retention. No external dependencies beyond Ubuntu's official repos and Python 3 stdlib.


## Architecture

pmacct runs two instances - one capturing on the WireGuard interface (wg0), the other on the main interface filtered to the Xray port. Each instance has two plugins: a memory plugin for real-time queries and a SQLite plugin for persistent hourly historical data.

The web dashboard reads from both data sources and presents a unified view. A CLI tool provides the same data for terminal use.

```
wg0 --> pmacctd-wg --> memory plugin --> live queries (pmacct CLI / dashboard API)
                   \-> sqlite3 plugin --> /var/lib/pmacct/wg.db --> historical queries

ens18 --> pmacctd-xray (precise filter, see below) --> memory plugin --> live queries
                                                   \-> sqlite3 plugin --> /var/lib/pmacct/xray.db
```

The xray pcap filter must be precise enough to capture only client-to-server traffic on the VPN listener ports (48721 for REALITY, 443 for XHTTP+Nginx). A naive `port 443` filter would also match xray's own upstream connections to the internet (ephemeral source port -> remote 443), which would pollute the dashboard with fake clients. The correct filter anchors every port clause with `host MAIN_IP` so only flows where the server's own listener port is an endpoint are captured. See step 3 below.


## Prerequisites

The server must have WireGuard and/or Xray already running. You need root access and the following information from your setup:

- Main network interface name (e.g., ens18)
- WireGuard interface name (e.g., wg0)
- Xray listening port (e.g., 48721)
- Server's LAN IP (e.g., 192.168.xx.xx)
- WireGuard server IP (e.g., 10.20.0.1)
- WireGuard subnet (e.g., 10.20.0.0/24)

Adjust these values throughout the guide to match your environment.


## Step 1: Install pmacct and dependencies

```bash
sudo apt update
sudo apt install -y pmacct sqlite3 bc python3-maxminddb
```

pmacct 1.7.8 from Ubuntu 24.04 repos includes the SQLite3 plugin, memory plugin, and the pmacct CLI client. The `python3-maxminddb` package provides GeoIP lookup support for the web dashboard.


## Step 2: Fix the Ubuntu 24.04 SQLite bug

pmacct 1.7.8 on Ubuntu 24.04 has a known bug (LP#2071608) where the SQLite3 plugin crashes immediately because it tries to reference `vlan_in` and `vlan_out` columns that don't exist in the default table schema. The fix is to pre-create the databases with a patched schema that includes these columns.

Create the data directory and initialize both databases:

```bash
sudo mkdir -p /var/lib/pmacct

# Create Xray traffic database
sudo sqlite3 /var/lib/pmacct/xray.db "CREATE TABLE acct (
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

# Create WireGuard traffic database (same schema)
sudo cp /var/lib/pmacct/xray.db /var/lib/pmacct/wg.db
```

Without this step, pmacct logs `connection lost to 'hist-sqlite3'` and never writes any data. This bug is specific to the Ubuntu 24.04 package and may be fixed in future versions.


## Step 3: Configure pmacct

Create the Xray traffic accounting config. Adjust `pcap_interface` and `pcap_filter` to match your setup:

Replace `192.168.2.11` with your server's own LAN/public IP in every `host` clause, and adjust the ports to match your setup. If you only run REALITY on port 48721, drop the port-443 clauses. If you run additional ingress ports, add one `(dst port N and dst host MAIN_IP) or (src port N and src host MAIN_IP)` pair per port.

```bash
sudo tee /etc/pmacct/pmacctd-xray.conf > /dev/null << 'EOF'
daemonize: true
pidfile: /run/pmacctd-xray.pid
pcap_interface: ens18
pcap_filter: (dst port 48721 and dst host 192.168.2.11) or (src port 48721 and src host 192.168.2.11) or (dst port 443 and dst host 192.168.2.11) or (src port 443 and src host 192.168.2.11)

# Large plugin pipe avoids silent "Missing data" drops under bursty load.
# The default 4MB pipe + 320-byte buffer is too tight for multi-client VPN
# traffic and eventually causes the plugin pipe to stall, freezing the
# SQLite3 plugin without any logged error.
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
EOF
```

Create the WireGuard traffic accounting config:

```bash
sudo tee /etc/pmacct/pmacctd-wg.conf > /dev/null << 'EOF'
daemonize: true
pidfile: /run/pmacctd-wg.pid
pcap_interface: wg0

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
EOF
```

Configuration notes: `sql_refresh_time: 60` flushes accumulated data to SQLite every 60 seconds. `sql_history: 1h` with `sql_history_roundoff: h` buckets data into hourly slots. The memory plugin provides instant access to current session data via the named pipe, while the SQLite plugin handles persistence. The `plugin_buffer_size` and `plugin_pipe_size` overrides are mandatory on multi-client VPN servers - the pmacct 1.7.8 defaults are too tight and cause the SQLite3 plugin to silently stall after several days of runtime (process stays alive but stops writing, no error in journal). See the troubleshooting section below.

If you only run one VPN protocol (WireGuard or Xray, not both), skip the config for the one you don't use and adjust the dashboard and CLI scripts accordingly.


## Step 4: Create systemd services

Xray accounting service:

```bash
sudo tee /etc/systemd/system/pmacctd-xray.service > /dev/null << 'EOF'
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
```

WireGuard accounting service:

```bash
sudo tee /etc/systemd/system/pmacctd-wg.service > /dev/null << 'EOF'
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
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pmacctd-xray pmacctd-wg
```


## Step 5: Verify data collection

After about 60 seconds (one flush cycle), verify that data is flowing:

```bash
# Check live data
pmacct -s -p /tmp/pmacct-xray.pipe

# Check SQLite has data (wait at least 60s after starting)
sudo sqlite3 /var/lib/pmacct/xray.db \
  "SELECT ip_src, ip_dst, packets, bytes, stamp_inserted FROM acct LIMIT 10;"
```

If the SQLite query returns no rows after a few minutes but the live query shows data, the vlan column fix from Step 2 was not applied correctly. Check with `sudo sqlite3 /var/lib/pmacct/xray.db '.schema'` and verify `vlan_in` and `vlan_out` columns exist.


## Step 6: Install the CLI tool

The `vpn-stats` script provides terminal-based traffic reports. Create it:

```bash
sudo tee /usr/local/bin/vpn-stats > /dev/null << 'SCRIPT'
#!/bin/bash
# VPN traffic statistics - queries pmacct SQLite databases

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
    echo ""
    echo "=== $label (Live) ==="
    print_header

    pmacct -s -p "$pipe" -O csv 2>/dev/null | tail -n +2 | \
    while IFS="," read -r src dst pkts bytes; do
        src=$(echo "$src" | xargs)
        dst=$(echo "$dst" | xargs)
        bytes=$(echo "$bytes" | xargs)
        if [ "$src" = "$server_ip" ]; then
            echo "OUT $dst $bytes"
        else
            echo "IN $src $bytes"
        fi
    done | awk '{
        ip=$2; b=$3
        if ($1=="IN") ins[ip]+=b
        else outs[ip]+=b
        seen[ip]=1
    }
    END {
        for (ip in seen) {
            i=ins[ip]+0; o=outs[ip]+0; t=i+o
            printf "%s %d %d %d\n", ip, i, o, t
        }
    }' | sort -t" " -k4 -rn | while read -r ip i o t; do
        printf "%-42s %12s %12s %12s\n" \
            "$ip" "$(fmt_bytes "$i")" "$(fmt_bytes "$o")" "$(fmt_bytes "$t")"
    done
}

query_hist() {
    local db=$1 label=$2 server_ip=$3 period="${4:-today}"
    local where=""

    case "$period" in
        today)     where="stamp_inserted >= date('now', 'start of day')" ;;
        yesterday) where="stamp_inserted >= date('now', '-1 day', 'start of day') AND stamp_inserted < date('now', 'start of day')" ;;
        week)      where="stamp_inserted >= date('now', '-7 days')" ;;
        month)     where="stamp_inserted >= date('now', 'start of month')" ;;
        lastmonth) where="stamp_inserted >= date('now', 'start of month', '-1 month') AND stamp_inserted < date('now', 'start of month')" ;;
        all)       where="1=1" ;;
        *)         where="stamp_inserted >= date('now', '-$period days')" ;;
    esac

    echo ""
    echo "=== $label ($period) ==="
    print_header

    sqlite3 -separator "|" "$db" "
        SELECT
            CASE WHEN ip_src = '$server_ip' THEN ip_dst ELSE ip_src END as client,
            SUM(CASE WHEN ip_src != '$server_ip' THEN bytes ELSE 0 END) as bytes_in,
            SUM(CASE WHEN ip_src = '$server_ip' THEN bytes ELSE 0 END) as bytes_out,
            SUM(bytes) as total
        FROM acct
        WHERE $where
        GROUP BY client
        ORDER BY total DESC;
    " 2>/dev/null | while IFS="|" read -r ip i o t; do
        [ -z "$ip" ] && continue
        printf "%-42s %12s %12s %12s\n" \
            "$ip" "$(fmt_bytes "$i")" "$(fmt_bytes "$o")" "$(fmt_bytes "$t")"
    done
}

case "${1:-live}" in
    live)
        query_live /tmp/pmacct-xray.pipe "Xray Clients" "192.168.2.11"
        query_live /tmp/pmacct-wg.pipe "WireGuard Peers" "10.20.0.1"
        ;;
    today|yesterday|week|month|lastmonth|all|[0-9]*)
        period=${1:-today}
        query_hist /var/lib/pmacct/xray.db "Xray Clients" "192.168.2.11" "$period"
        query_hist /var/lib/pmacct/wg.db "WireGuard Peers" "10.20.0.1" "$period"
        ;;
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
SCRIPT
sudo chmod +x /usr/local/bin/vpn-stats
```

Adjust the server IPs (192.168.2.11, 10.20.0.1) inside the script to match your environment.

Usage:

```bash
vpn-stats              # live data (current session)
vpn-stats today        # today's accumulated traffic
vpn-stats week         # last 7 days
vpn-stats month        # this calendar month
vpn-stats lastmonth    # previous month
vpn-stats all          # all time
vpn-stats 30           # last 30 days
```


## Step 7: Install GeoIP databases

The dashboard uses free DB-IP city-lite databases (MMDB format) to resolve client IP addresses to country flags and city names. The databases are updated monthly by DB-IP under a Creative Commons license.

```bash
sudo mkdir -p /var/lib/GeoIP

# Download current month's databases (replace 2026-03 with current year-month)
MONTH=$(date +%Y-%m)
sudo bash -c "cd /var/lib/GeoIP && \
  curl -sL \"https://download.db-ip.com/free/dbip-city-lite-${MONTH}.mmdb.gz\" | gunzip > dbip-city-lite.mmdb && \
  curl -sL \"https://download.db-ip.com/free/dbip-country-lite-${MONTH}.mmdb.gz\" | gunzip > dbip-country-lite.mmdb"

ls -lh /var/lib/GeoIP/
```

The city database is about 126 MB, the country database about 7 MB. To keep the databases current, set up a monthly cron job:

```bash
echo '0 3 2 * * root MONTH=$(date +\%Y-\%m); cd /var/lib/GeoIP && curl -sL "https://download.db-ip.com/free/dbip-city-lite-${MONTH}.mmdb.gz" | gunzip > dbip-city-lite.mmdb.tmp && mv dbip-city-lite.mmdb.tmp dbip-city-lite.mmdb && systemctl restart vpn-dashboard' | sudo tee /etc/cron.d/geoip-update > /dev/null
```

If the GeoIP databases are not installed, the dashboard still works - it just won't show location data.


## Step 8: Install the web dashboard

The dashboard is a single Python script using only stdlib (plus `maxminddb` for GeoIP). It reads from pmacct's memory plugin for live data, SQLite databases for historical data, and the MMDB files for geolocation.

```bash
sudo mkdir -p /opt/vpn-dashboard
```

Download `vpn-dashboard.py` from the project repository into `/opt/vpn-dashboard/`. Before running, edit the constants at the top of the file to match your environment:

```python
PORT = 3000                  # web UI port
XRAY_DB = "/var/lib/pmacct/xray.db"
WG_DB = "/var/lib/pmacct/wg.db"
XRAY_PIPE = "/tmp/pmacct-xray.pipe"
WG_PIPE = "/tmp/pmacct-wg.pipe"
SERVER_IP_XRAY = "192.168.2.11"   # your server's LAN IP
SERVER_IP_WG = "10.20.0.1"        # your WireGuard server IP
RETENTION_DAYS = 180               # 6 months
GEOIP_DB = "/var/lib/GeoIP/dbip-city-lite.mmdb"  # GeoIP database path
```

Create the systemd service:

```bash
sudo tee /etc/systemd/system/vpn-dashboard.service > /dev/null << 'EOF'
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

sudo systemctl daemon-reload
sudo systemctl enable --now vpn-dashboard
```

Access the dashboard at `http://<server-ip>:3000`. No authentication is required - restrict access at the firewall level if the server is exposed to untrusted networks.

Dashboard features: real-time per-host bandwidth rates updated every 5 seconds, combined WireGuard and Xray client table sorted by traffic volume, GeoIP location with country flags and city names, period selector (Live, Today, Yesterday, This Week, This Month, Last Month, All Time), interactive throughput chart, reverse DNS resolution for client IPs, and automatic 6-month data retention cleanup.


## Resource usage

The complete stack is lightweight. pmacct uses about 20 MB RSS per instance. The Python dashboard uses about 15 MB. SQLite databases grow slowly - a few MB per month depending on the number of unique client IPs. Total overhead is under 60 MB RAM and negligible CPU.


## Maintenance

Check service status:

```bash
systemctl status pmacctd-xray pmacctd-wg vpn-dashboard
```

Database sizes:

```bash
ls -lh /var/lib/pmacct/
```

Manual data cleanup (normally handled automatically by the dashboard):

```bash
sudo sqlite3 /var/lib/pmacct/xray.db \
  "DELETE FROM acct WHERE stamp_inserted < date('now', '-180 days'); VACUUM;"
sudo sqlite3 /var/lib/pmacct/wg.db \
  "DELETE FROM acct WHERE stamp_inserted < date('now', '-180 days'); VACUUM;"
```

If the SQLite databases become corrupted (e.g., after an unclean shutdown), delete them, re-run the schema creation from Step 2, and restart the pmacctd services. Live data continues uninterrupted since it uses the in-memory plugin.


## Troubleshooting

**pmacct shows no live data**: verify the interface name with `ip link show` and that your VPN service is actually running. For Xray, confirm the listening port with `ss -tlnp | grep <port>`.

**SQLite database stays at 0 bytes**: the Ubuntu 24.04 vlan column bug. Delete the DB, re-create with the patched schema from Step 2, and restart the pmacctd service.

**Dashboard shows "No traffic data"**: wait at least 60 seconds for the first SQLite flush. Check that pmacctd services are running and that `pmacct -s -p /tmp/pmacct-xray.pipe` returns data.

**Dashboard not accessible**: check that port 3000 is not blocked by the firewall and that the vpn-dashboard service is running. The default INPUT policy must be ACCEPT, or add an explicit rule for port 3000.

**SQLite DB stops writing after several days (silent hang)**: pmacct 1.7.8's default `plugin_pipe_size=4MB` and `plugin_buffer_size=320` bytes are too tight for multi-client VPN traffic. Over time the internal pipe fills, the core process starts dropping writes to the plugin with "Failed during write: Resource temporarily unavailable" warnings, and eventually the SQLite3 child process stops processing its queue entirely. The process stays alive (systemctl shows active, wchan=0 on the plugin PID) and nothing gets logged to journalctl, so it looks fine from the outside. The memory plugin keeps accumulating until restart. To fix, add the following lines before the `plugins:` directive in both `/etc/pmacct/pmacctd-xray.conf` and `/etc/pmacct/pmacctd-wg.conf` and restart the services:

```
plugin_buffer_size: 10240
plugin_pipe_size: 104857600
```

These are already in the step 3 configs above, but existing deployments from earlier versions of this guide may be missing them.

**Dashboard shows only a subset of clients, or no XHTTP clients**: the pcap filter in `/etc/pmacct/pmacctd-xray.conf` captures only traffic on specific listener ports. If you have multiple ingress paths (e.g. REALITY on 48721 and XHTTP+Nginx on 443), the filter must include all of them. See step 3 for the filter shape. Run `pmacctd -f /etc/pmacct/pmacctd-xray.conf` manually (with `daemonize: false` set temporarily) to see the parsed filter and any libpcap errors. Confirm real traffic matches the filter by swapping the memory plugin for a print plugin and watching the output - if the print plugin shows the clients you expect, the capture side is fine and any dashboard-side issue is in the aggregation code.

**Dashboard shows fake "clients" that are actually internet destinations**: your pcap filter is matching xray's upstream connections (e.g. server_ephemeral_port -> remote:443). Every port clause in the filter must be anchored with `and dst host MAIN_IP` or `and src host MAIN_IP` so only flows where the server's own listener port is an endpoint get captured. See step 3.

**VACUUM runs in the dashboard stall pmacct**: earlier versions of `vpn-dashboard.py` ran `VACUUM` on the DB files inside `cleanup_old_data()`. VACUUM requires an exclusive lock, which conflicts with pmacctd's persistent SQLite connection and can cause write stalls. The current version uses plain `DELETE` with a 2-second busy timeout and no VACUUM - keep it that way.
