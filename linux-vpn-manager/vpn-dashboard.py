#!/usr/bin/env python3
"""VPN Traffic Dashboard - web UI for pmacct traffic data."""

import http.server
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

PORT = 3000
XRAY_DB = "/var/lib/pmacct/xray.db"
WG_DB = "/var/lib/pmacct/wg.db"
XRAY_PIPE = "/tmp/pmacct-xray.pipe"
WG_PIPE = "/tmp/pmacct-wg.pipe"
SERVER_IP_XRAY = "192.168.2.11"  # Must match pcap_filter in /etc/pmacct/pmacctd-xray.conf
SERVER_IP_WG = "10.20.0.1"
WG_SUBNET = "10.20.0."
POLL_INTERVAL = 5
GRAPH_POINTS = 60  # 5 minutes of history at 5s intervals
RETENTION_DAYS = 180  # 6 months
MIN_TRAFFIC_BYTES = 64 * 1024  # 64 KB floor - spam filter (zero in/out) handles scanners
GEOIP_DB = "/var/lib/GeoIP/dbip-city-lite.mmdb"

# DNS cache
dns_cache = {}
dns_lock = threading.Lock()

# GeoIP
geo_cache = {}
geo_lock = threading.Lock()
geo_reader = None

try:
    import maxminddb
    geo_reader = maxminddb.open_database(GEOIP_DB)
except Exception:
    pass


def geoip_lookup(ip):
    with geo_lock:
        if ip in geo_cache:
            return geo_cache[ip]
    result = {"country": "", "country_code": "", "city": ""}
    if not geo_reader:
        return result
    try:
        data = geo_reader.get(ip)
        if data:
            country = data.get("country", {})
            city = data.get("city", {})
            result["country"] = country.get("names", {}).get("en", "")
            result["country_code"] = country.get("iso_code", "")
            result["city"] = city.get("names", {}).get("en", "")
    except Exception:
        pass
    with geo_lock:
        geo_cache[ip] = result
    return result

# Rate history for graphs
rate_history = []
rate_lock = threading.Lock()
prev_totals = {"xray": {}, "wg": {}}


def resolve_host(ip):
    with dns_lock:
        if ip in dns_cache:
            return dns_cache[ip]
    try:
        hostname = socket.gethostbyaddr(ip)[0]
    except (socket.herror, socket.gaierror, OSError):
        hostname = ""
    with dns_lock:
        dns_cache[ip] = hostname
    return hostname


def query_pmacct_live(pipe):
    try:
        result = subprocess.run(
            ["pmacct", "-s", "-p", pipe, "-O", "csv"],
            capture_output=True, text=True, timeout=5,
        )
        rows = []
        for line in result.stdout.strip().split("\n")[1:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                rows.append({
                    "src": parts[0], "dst": parts[1],
                    "packets": int(parts[2]), "bytes": int(parts[3]),
                })
        return rows
    except Exception:
        return []


def _classify_xray(src, dst, server_ip):
    """For Xray: server terminates connections on server_ip. Return (peer_ip, direction) or None."""
    if src == server_ip:
        return dst, "out"  # server -> client (download from client's view)
    if dst == server_ip:
        return src, "in"   # client -> server (upload from client's view)
    return None


def _classify_wg(src, dst):
    """For WG: client is whichever side is in WG_SUBNET. Return (client_ip, direction) or None."""
    src_is_client = src.startswith(WG_SUBNET)
    dst_is_client = dst.startswith(WG_SUBNET)
    if src_is_client and not dst_is_client:
        return src, "out"  # client -> external (upload from client)
    if dst_is_client and not src_is_client:
        return dst, "in"   # external -> client (download to client)
    return None  # skip server<->client admin traffic and client<->client


def aggregate_live(rows, classifier):
    hosts = defaultdict(lambda: {"in_bytes": 0, "out_bytes": 0, "in_packets": 0, "out_packets": 0})
    for r in rows:
        result = classifier(r["src"], r["dst"])
        if result is None:
            continue
        peer, direction = result
        hosts[peer][f"{direction}_bytes"] += r["bytes"]
        hosts[peer][f"{direction}_packets"] += r["packets"]
    return hosts


def get_live_data():
    xray_rows = query_pmacct_live(XRAY_PIPE)
    wg_rows = query_pmacct_live(WG_PIPE)
    xray_hosts = aggregate_live(xray_rows, lambda s, d: _classify_xray(s, d, SERVER_IP_XRAY))
    wg_hosts = aggregate_live(wg_rows, _classify_wg)

    combined = []
    for ip, data in xray_hosts.items():
        total = data["in_bytes"] + data["out_bytes"]
        geo = geoip_lookup(ip)
        combined.append({
            "ip": ip, "hostname": resolve_host(ip),
            "type": "Xray", "in": data["in_bytes"], "out": data["out_bytes"],
            "total": total, "packets": data["in_packets"] + data["out_packets"],
            "country": geo["country"], "country_code": geo["country_code"], "city": geo["city"],
        })
    for ip, data in wg_hosts.items():
        total = data["in_bytes"] + data["out_bytes"]
        geo = geoip_lookup(ip)
        combined.append({
            "ip": ip, "hostname": resolve_host(ip),
            "type": "WG", "in": data["in_bytes"], "out": data["out_bytes"],
            "total": total, "packets": data["in_packets"] + data["out_packets"],
            "country": geo["country"], "country_code": geo["country_code"], "city": geo["city"],
        })
    combined.sort(key=lambda x: x["total"], reverse=True)
    # Spam filter: drop zero-direction flows (scanners/probes) and tiny totals
    combined = [h for h in combined if h["in"] > 0 and h["out"] > 0 and h["total"] >= MIN_TRAFFIC_BYTES]
    return combined


def get_period_sql(period):
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return f"stamp_inserted >= '{start.strftime('%Y-%m-%d %H:%M:%S')}'"
    elif period == "yesterday":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return f"stamp_inserted >= '{start.strftime('%Y-%m-%d %H:%M:%S')}' AND stamp_inserted < '{end.strftime('%Y-%m-%d %H:%M:%S')}'"
    elif period == "week":
        start = now - timedelta(days=7)
        return f"stamp_inserted >= '{start.strftime('%Y-%m-%d %H:%M:%S')}'"
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return f"stamp_inserted >= '{start.strftime('%Y-%m-%d %H:%M:%S')}'"
    elif period == "lastmonth":
        first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        first_last = (first_this - timedelta(days=1)).replace(day=1)
        return f"stamp_inserted >= '{first_last.strftime('%Y-%m-%d %H:%M:%S')}' AND stamp_inserted < '{first_this.strftime('%Y-%m-%d %H:%M:%S')}'"
    elif period == "all":
        return "1=1"
    elif period.isdigit():
        start = now - timedelta(days=int(period))
        return f"stamp_inserted >= '{start.strftime('%Y-%m-%d %H:%M:%S')}'"
    return "1=1"


def _xray_direction_clauses(server_ip):
    """Return (in_clause, out_clause, client_expr) for Xray: server_ip is fixed."""
    in_clause = f"CASE WHEN ip_dst = '{server_ip}' THEN bytes ELSE 0 END"
    out_clause = f"CASE WHEN ip_src = '{server_ip}' THEN bytes ELSE 0 END"
    client_expr = f"CASE WHEN ip_src = '{server_ip}' THEN ip_dst ELSE ip_src END"
    filter_expr = f"(ip_src = '{server_ip}' OR ip_dst = '{server_ip}')"
    return in_clause, out_clause, client_expr, filter_expr


def _wg_direction_clauses(subnet):
    """Return (in_clause, out_clause, client_expr) for WG: client is the side inside subnet."""
    # in = external -> client (dst inside subnet, src outside)
    in_clause = (
        f"CASE WHEN ip_dst LIKE '{subnet}%' AND ip_src NOT LIKE '{subnet}%' "
        f"THEN bytes ELSE 0 END"
    )
    # out = client -> external (src inside subnet, dst outside)
    out_clause = (
        f"CASE WHEN ip_src LIKE '{subnet}%' AND ip_dst NOT LIKE '{subnet}%' "
        f"THEN bytes ELSE 0 END"
    )
    client_expr = (
        f"CASE WHEN ip_src LIKE '{subnet}%' THEN ip_src ELSE ip_dst END"
    )
    # Only rows where exactly one side is in subnet
    filter_expr = (
        f"((ip_src LIKE '{subnet}%' AND ip_dst NOT LIKE '{subnet}%') "
        f"OR (ip_dst LIKE '{subnet}%' AND ip_src NOT LIKE '{subnet}%'))"
    )
    return in_clause, out_clause, client_expr, filter_expr


def query_history(db_path, vpn_type, period):
    if vpn_type == "Xray":
        in_c, out_c, client_c, filter_c = _xray_direction_clauses(SERVER_IP_XRAY)
    else:
        in_c, out_c, client_c, filter_c = _wg_direction_clauses(WG_SUBNET)
    where = get_period_sql(period)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(f"""
            SELECT
                {client_c} AS client,
                SUM({in_c}) AS bytes_in,
                SUM({out_c}) AS bytes_out,
                SUM({in_c}) + SUM({out_c}) AS total,
                SUM(packets) AS pkts
            FROM acct
            WHERE {where} AND {filter_c}
            GROUP BY client
            ORDER BY total DESC
        """)
        rows = []
        for row in cur.fetchall():
            geo = geoip_lookup(row[0])
            rows.append({
                "ip": row[0], "hostname": resolve_host(row[0]),
                "type": vpn_type, "in": row[1], "out": row[2],
                "total": row[3], "packets": row[4],
                "country": geo["country"], "country_code": geo["country_code"], "city": geo["city"],
            })
        conn.close()
        return rows
    except Exception:
        return []


def get_history_data(period):
    xray = query_history(XRAY_DB, "Xray", period)
    wg = query_history(WG_DB, "WG", period)
    combined = xray + wg
    combined.sort(key=lambda x: x["total"], reverse=True)
    # Spam filter: drop zero-direction flows and tiny totals
    combined = [h for h in combined if h["in"] > 0 and h["out"] > 0 and h["total"] >= MIN_TRAFFIC_BYTES]
    return combined


def get_graph_timeseries(db_path, vpn_type, period):
    if vpn_type == "Xray":
        in_c, out_c, _, filter_c = _xray_direction_clauses(SERVER_IP_XRAY)
    else:
        in_c, out_c, _, filter_c = _wg_direction_clauses(WG_SUBNET)
    where = get_period_sql(period)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(f"""
            SELECT stamp_inserted,
                   SUM({in_c}) AS bytes_in,
                   SUM({out_c}) AS bytes_out
            FROM acct
            WHERE {where} AND {filter_c}
            GROUP BY stamp_inserted
            ORDER BY stamp_inserted
        """)
        points = [{"t": row[0], "in": row[1], "out": row[2]} for row in cur.fetchall()]
        conn.close()
        return points
    except Exception:
        return []


def cleanup_old_data():
    # Delete rows older than retention. Skip VACUUM to avoid exclusive-lock
    # contention with pmacctd's SQLite3 plugin (pmacctd has a persistent
    # connection and VACUUM requires exclusive access, which has been observed
    # to silently stall pmacct writes).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    for db_path in [XRAY_DB, WG_DB]:
        try:
            # Use short busy timeout so we back off quickly if pmacctd is writing
            conn = sqlite3.connect(db_path, timeout=2.0)
            conn.execute(f"DELETE FROM acct WHERE stamp_inserted < '{cutoff}'")
            conn.commit()
            conn.close()
        except Exception:
            pass


def rate_collector():
    global prev_totals
    while True:
        try:
            totals = {"in": 0, "out": 0, "ts": time.time()}
            # Xray: classify by server_ip equality
            for r in query_pmacct_live(XRAY_PIPE):
                result = _classify_xray(r["src"], r["dst"], SERVER_IP_XRAY)
                if result is None:
                    continue
                _, direction = result
                totals[direction] += r["bytes"]
            # WG: classify by subnet membership
            for r in query_pmacct_live(WG_PIPE):
                result = _classify_wg(r["src"], r["dst"])
                if result is None:
                    continue
                _, direction = result
                totals[direction] += r["bytes"]

            with rate_lock:
                if rate_history:
                    prev = rate_history[-1]
                    dt = totals["ts"] - prev["ts"]
                    if dt > 0:
                        rate_in = max(0, (totals["in"] - prev["raw_in"])) / dt
                        rate_out = max(0, (totals["out"] - prev["raw_out"])) / dt
                    else:
                        rate_in = rate_out = 0
                else:
                    # First sample: no previous baseline, skip publishing a rate
                    rate_in = rate_out = 0

                rate_history.append({
                    "ts": totals["ts"],
                    "raw_in": totals["in"],
                    "raw_out": totals["out"],
                    "rate_in": rate_in,
                    "rate_out": rate_out,
                    "first": not rate_history,  # flag the initial baseline point
                })
                if len(rate_history) > GRAPH_POINTS:
                    rate_history.pop(0)
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


def cleanup_scheduler():
    while True:
        time.sleep(86400)  # daily
        cleanup_old_data()


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VPN Traffic Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; }
.header { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 20px; font-weight: 600; color: #58a6ff; }
.stats-bar { display: flex; gap: 24px; }
.stat { text-align: center; }
.stat-value { font-size: 18px; font-weight: 600; color: #e6edf3; }
.stat-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
.container { max-width: 1400px; margin: 0 auto; padding: 20px; }
.controls { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.controls button { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; transition: all 0.15s; }
.controls button:hover { background: #30363d; border-color: #8b949e; }
.controls button.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }
.chart-container { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 20px; height: 220px; }
table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
th { background: #1c2128; padding: 10px 16px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #8b949e; border-bottom: 1px solid #30363d; cursor: pointer; user-select: none; }
th:hover { color: #c9d1d9; }
td { padding: 10px 16px; border-bottom: 1px solid #21262d; font-size: 14px; }
tr:hover td { background: #1c2128; }
.type-xray { color: #f0883e; font-weight: 600; }
.type-wg { color: #3fb950; font-weight: 600; }
.bytes-in { color: #58a6ff; }
.bytes-out { color: #f0883e; }
.total { color: #e6edf3; font-weight: 600; }
.hostname { color: #8b949e; font-size: 12px; }
.geo { color: #8b949e; font-size: 12px; }
.flag { font-size: 16px; margin-right: 4px; }
.ip-cell { display: flex; flex-direction: column; }
.host-line { display: flex; align-items: center; gap: 6px; }
.rank { color: #484f58; font-weight: 600; min-width: 24px; text-align: center; }
.footer { text-align: center; padding: 16px; color: #484f58; font-size: 12px; }
.no-data { text-align: center; padding: 40px; color: #484f58; }
@media (max-width: 768px) {
    .stats-bar { gap: 12px; }
    td, th { padding: 6px 8px; font-size: 12px; }
}
</style>
</head>
<body>
<div class="header">
    <h1>VPN Traffic Monitor</h1>
    <div class="stats-bar">
        <div class="stat"><div class="stat-value bytes-in" id="rate-in">--</div><div class="stat-label">Download</div></div>
        <div class="stat"><div class="stat-value bytes-out" id="rate-out">--</div><div class="stat-label">Upload</div></div>
        <div class="stat"><div class="stat-value" id="total-traffic">--</div><div class="stat-label">Total</div></div>
        <div class="stat"><div class="stat-value" id="host-count">--</div><div class="stat-label">Hosts</div></div>
    </div>
</div>

<div class="container">
    <div class="controls">
        <button class="active" data-period="live">Live</button>
        <button data-period="today">Today</button>
        <button data-period="yesterday">Yesterday</button>
        <button data-period="week">This Week</button>
        <button data-period="month">This Month</button>
        <button data-period="lastmonth">Last Month</button>
        <button data-period="all">All Time</button>
    </div>

    <div class="chart-container">
        <canvas id="rateChart"></canvas>
    </div>

    <table>
        <thead>
            <tr>
                <th style="width:40px">#</th>
                <th data-sort="ip">Host</th>
                <th data-sort="country" style="width:180px">Location</th>
                <th data-sort="type" style="width:70px">Type</th>
                <th data-sort="in" style="width:120px">In</th>
                <th data-sort="out" style="width:120px">Out</th>
                <th data-sort="total" style="width:120px">Total</th>
            </tr>
        </thead>
        <tbody id="host-table"></tbody>
    </table>
    <div class="footer" id="footer">pmacct + SQLite | Refreshing every 5s</div>
</div>

<script>
let currentPeriod = 'live';
let sortField = 'total';
let sortAsc = false;
let currentData = [];

function fmtBytes(b) {
    if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' GB';
    if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
    if (b >= 1024) return (b / 1024).toFixed(0) + ' KB';
    return b + ' B';
}

function ccToFlag(cc) {
    if (!cc || cc.length !== 2) return '';
    const offset = 0x1F1E6 - 65;
    return String.fromCodePoint(cc.charCodeAt(0) + offset, cc.charCodeAt(1) + offset);
}

function fmtRate(bps) {
    if (bps >= 1048576) return (bps * 8 / 1048576).toFixed(1) + ' Mbps';
    if (bps >= 1024) return (bps * 8 / 1024).toFixed(1) + ' Kbps';
    return (bps * 8).toFixed(0) + ' bps';
}

const ctx = document.getElementById('rateChart').getContext('2d');
const chart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: [],
        datasets: [
            { label: 'Download', data: [], borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.1)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 },
            { label: 'Upload', data: [], borderColor: '#f0883e', backgroundColor: 'rgba(240,136,62,0.1)', fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 },
        ]
    },
    options: {
        responsive: true, maintainAspectRatio: false,
        animation: { duration: 0 },
        scales: {
            x: { display: true, grid: { color: '#21262d' }, ticks: { color: '#484f58', maxTicksLimit: 10 } },
            y: { display: true, grid: { color: '#21262d' }, ticks: { color: '#484f58', callback: v => fmtRate(v) }, beginAtZero: true },
        },
        plugins: {
            legend: { display: true, labels: { color: '#8b949e', boxWidth: 12, padding: 16 } },
            tooltip: { callbacks: { label: ctx => ctx.dataset.label + ': ' + fmtRate(ctx.raw) } },
        },
        interaction: { intersect: false, mode: 'index' },
    }
});

function renderTable(data) {
    currentData = data;
    data.sort((a, b) => sortAsc ? (a[sortField] > b[sortField] ? 1 : -1) : (a[sortField] < b[sortField] ? 1 : -1));
    const tbody = document.getElementById('host-table');
    if (!data.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="no-data">No traffic data for this period</td></tr>';
        return;
    }
    let totalIn = 0, totalOut = 0, totalAll = 0;
    let html = '';
    data.forEach((h, i) => {
        totalIn += h.in; totalOut += h.out; totalAll += h.total;
        const typeClass = h.type === 'Xray' ? 'type-xray' : 'type-wg';
        const hostname = h.hostname ? '<span class="hostname">' + h.hostname + '</span>' : '';
        const flag = h.country_code ? ccToFlag(h.country_code) : '';
        const loc = [h.city, h.country].filter(Boolean).join(', ');
        const locHtml = loc ? '<span class="flag">' + flag + '</span><span class="geo">' + loc + '</span>' : '';
        html += '<tr>'
            + '<td class="rank">' + (i + 1) + '</td>'
            + '<td><div class="ip-cell"><div class="host-line"><span>' + h.ip + '</span></div>' + hostname + '</div></td>'
            + '<td>' + locHtml + '</td>'
            + '<td><span class="' + typeClass + '">' + h.type + '</span></td>'
            + '<td class="bytes-in">' + fmtBytes(h.in) + '</td>'
            + '<td class="bytes-out">' + fmtBytes(h.out) + '</td>'
            + '<td class="total">' + fmtBytes(h.total) + '</td>'
            + '</tr>';
    });
    tbody.innerHTML = html;
    document.getElementById('total-traffic').textContent = fmtBytes(totalAll);
    document.getElementById('host-count').textContent = data.length;
}

async function fetchData() {
    try {
        if (currentPeriod === 'live') {
            const [hostResp, rateResp] = await Promise.all([
                fetch('/api/live'), fetch('/api/rates')
            ]);
            const hosts = await hostResp.json();
            const rates = await rateResp.json();
            renderTable(hosts);

            if (rates.history && rates.history.length) {
                chart.data.labels = rates.history.map(p => {
                    const d = new Date(p.ts * 1000);
                    return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0') + ':' + d.getSeconds().toString().padStart(2,'0');
                });
                chart.data.datasets[0].data = rates.history.map(p => p.rate_in);
                chart.data.datasets[1].data = rates.history.map(p => p.rate_out);
                chart.update();

                const last = rates.history[rates.history.length - 1];
                document.getElementById('rate-in').textContent = fmtRate(last.rate_in);
                document.getElementById('rate-out').textContent = fmtRate(last.rate_out);
            }
        } else {
            const resp = await fetch('/api/history?period=' + currentPeriod);
            const data = await resp.json();
            renderTable(data);

            // Show historical graph
            const graphResp = await fetch('/api/graph?period=' + currentPeriod);
            const graphData = await graphResp.json();
            if (graphData.xray || graphData.wg) {
                const points = [...(graphData.xray || []), ...(graphData.wg || [])];
                const byTime = {};
                points.forEach(p => {
                    if (!byTime[p.t]) byTime[p.t] = {in: 0, out: 0};
                    byTime[p.t].in += p.in;
                    byTime[p.t].out += p.out;
                });
                const times = Object.keys(byTime).sort();
                chart.data.labels = times.map(t => t.substring(5, 16));
                chart.data.datasets[0].data = times.map(t => byTime[t].in);
                chart.data.datasets[1].data = times.map(t => byTime[t].out);
                chart.options.scales.y.ticks.callback = v => fmtBytes(v);
                chart.update();
            }

            let totalIn = 0, totalOut = 0;
            data.forEach(h => { totalIn += h.in; totalOut += h.out; });
            document.getElementById('rate-in').textContent = fmtBytes(totalIn);
            document.getElementById('rate-out').textContent = fmtBytes(totalOut);
        }
    } catch (e) {
        console.error('Fetch error:', e);
    }
}

document.querySelectorAll('.controls button').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.controls button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentPeriod = btn.dataset.period;
        if (currentPeriod === 'live') {
            chart.options.scales.y.ticks.callback = v => fmtRate(v);
        }
        fetchData();
    });
});

document.querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
        const field = th.dataset.sort;
        if (sortField === field) sortAsc = !sortAsc;
        else { sortField = field; sortAsc = false; }
        renderTable(currentData);
    });
});

fetchData();
setInterval(() => { if (currentPeriod === 'live') fetchData(); }, 5000);
</script>
</body>
</html>"""


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access logs

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/":
            body = HTML_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/api/live":
            self.send_json(get_live_data())

        elif parsed.path == "/api/history":
            period = params.get("period", ["today"])[0]
            self.send_json(get_history_data(period))

        elif parsed.path == "/api/rates":
            with rate_lock:
                history = [{"ts": p["ts"], "rate_in": p["rate_in"], "rate_out": p["rate_out"]} for p in rate_history]
            self.send_json({"history": history})

        elif parsed.path == "/api/graph":
            period = params.get("period", ["today"])[0]
            xray_points = get_graph_timeseries(XRAY_DB, "Xray", period)
            wg_points = get_graph_timeseries(WG_DB, "WG", period)
            self.send_json({"xray": xray_points, "wg": wg_points})

        else:
            self.send_response(404)
            self.end_headers()


def main():
    # Start background threads
    threading.Thread(target=rate_collector, daemon=True).start()
    threading.Thread(target=cleanup_scheduler, daemon=True).start()

    # Initial cleanup
    cleanup_old_data()

    server = http.server.HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"VPN Dashboard running on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
