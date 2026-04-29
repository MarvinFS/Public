#!/bin/bash
#
# XRay XHTTP+Nginx - Install & Manage
#
# VLESS+XHTTP transport behind Nginx reverse proxy with decoy website.
# Uses Let's Encrypt TLS, Xray on Unix socket, Nginx as TLS terminator.
# Designed for heavily censored networks (Russia TSPU, etc.)
#
# Compatible with v2rayNG, v2rayN, NekoBox, Hiddify
#
# Prerequisites (one-time):
#   1. Point your domain's A record to this server's public IP
#   2. Open ports 80 and 443 in your firewall
#   3. Run this script and follow the prompts
#

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh" || { echo "ERROR: common.sh not found"; exit 1; }

XHTTP_NGINX_DIR="/usr/local/etc/xray-xhttp-nginx"
XHTTP_NGINX_CONFIG="${XHTTP_NGINX_DIR}/config.json"
XHTTP_NGINX_PARAMS="${XHTTP_NGINX_DIR}/params"
CLIENT_DIR="/etc/vpn/xhttp-nginx/clients"
XRAY_SOCKET="/dev/shm/xray-xhttp-nginx.sock"
NGINX_SITE="/etc/nginx/sites-available/xhttp-nginx"
NGINX_SITE_ENABLED="/etc/nginx/sites-enabled/xhttp-nginx"
WEBSITE_DIR="/var/www/xhttp-nginx"

# Defaults
DEFAULT_XHTTP_PATH="/ingest/v2/"
DEFAULT_FINGERPRINT="chrome"


# ============================================================================
# INSTALLATION FUNCTIONS
# ============================================================================

install_xray_binary() {
    if command -v xray &>/dev/null || [[ -x /usr/local/bin/xray ]]; then
        export PATH="/usr/local/bin:$PATH"
        log_success "XRay already installed: $(xray version | head -1)"
        return 0
    fi

    log_info "Installing XRay via official script..."
    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install || true

    if ! command -v xray &>/dev/null && [[ ! -x /usr/local/bin/xray ]]; then
        log_error "XRay installation failed - binary not found"
        exit 1
    fi

    export PATH="/usr/local/bin:$PATH"
    log_success "XRay installed: $(xray version | head -1)"
}

install_nginx() {
    if command -v nginx &>/dev/null; then
        log_success "Nginx already installed: $(nginx -v 2>&1 | head -1)"
    else
        log_info "Installing Nginx..."
        case ${PKG_MANAGER} in
            apt-get) apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y nginx ;;
            dnf) dnf install -y nginx ;;
        esac
        if ! command -v nginx &>/dev/null; then
            log_error "Nginx installation failed"
            exit 1
        fi
        log_success "Nginx installed: $(nginx -v 2>&1 | head -1)"
    fi

    # Ensure sites-available/sites-enabled directories exist (RHEL/Rocky don't have them by default)
    mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
    if ! grep -q "sites-enabled" /etc/nginx/nginx.conf 2>/dev/null; then
        log_info "Adding sites-enabled include to nginx.conf..."
        sed -i '/http {/a\    include /etc/nginx/sites-enabled/*;' /etc/nginx/nginx.conf
    fi

    systemctl enable nginx
    systemctl start nginx 2>/dev/null || true

    # Install certbot
    if command -v certbot &>/dev/null; then
        log_success "Certbot already installed: $(certbot --version 2>&1 | head -1)"
    else
        log_info "Installing Certbot..."
        case ${PKG_MANAGER} in
            apt-get)
                DEBIAN_FRONTEND=noninteractive apt-get install -y certbot python3-certbot-nginx
                ;;
            dnf)
                dnf install -y epel-release 2>/dev/null || true
                dnf install -y certbot python3-certbot-nginx
                ;;
        esac
        if ! command -v certbot &>/dev/null; then
            log_error "Certbot installation failed"
            exit 1
        fi
        log_success "Certbot installed: $(certbot --version 2>&1 | head -1)"
    fi
}

setup_letsencrypt() {
    local domain="$1"

    if [[ -f "/etc/letsencrypt/live/${domain}/fullchain.pem" ]]; then
        log_success "Let's Encrypt certificate already exists for ${domain}"
        return 0
    fi

    log_info "Obtaining Let's Encrypt certificate for ${domain}..."

    # Create webroot for ACME challenge before certbot runs
    mkdir -p "${WEBSITE_DIR}/.well-known/acme-challenge"

    # Create a temporary minimal nginx config so certbot --nginx works
    cat > "${NGINX_SITE}" << EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${domain};
    location /.well-known/acme-challenge/ { root ${WEBSITE_DIR}; }
    location / { return 200 'ok'; }
}
EOF
    ln -sf "${NGINX_SITE}" "${NGINX_SITE_ENABLED}"
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
    nginx -t && systemctl reload nginx

    certbot --nginx -d "${domain}" --non-interactive --agree-tos --register-unsafely-without-email \
        --redirect 2>/dev/null || \
    certbot certonly --webroot -w "${WEBSITE_DIR}" -d "${domain}" --non-interactive \
        --agree-tos --register-unsafely-without-email

    if [[ ! -f "/etc/letsencrypt/live/${domain}/fullchain.pem" ]]; then
        log_error "Failed to obtain Let's Encrypt certificate for ${domain}"
        log_error "Ensure the domain's A record points to this server and ports 80/443 are open"
        exit 1
    fi

    log_success "Certificate obtained for ${domain}"
}

create_xhttp_nginx_service() {
    log_info "Creating xray-xhttp-nginx systemd service..."

    systemctl stop xray-xhttp-nginx 2>/dev/null || true
    systemctl disable xray-xhttp-nginx 2>/dev/null || true

    mkdir -p "${XHTTP_NGINX_DIR}" "${CLIENT_DIR}"
    chmod 700 "${XHTTP_NGINX_DIR}" "${CLIENT_DIR}"

    cat > /etc/systemd/system/xray-xhttp-nginx.service << EOF
[Unit]
Description=XRay XHTTP (Nginx) Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
LimitNOFILE=32768
ExecStartPre=/bin/rm -f ${XRAY_SOCKET}
ExecStart=/usr/local/bin/xray run -config ${XHTTP_NGINX_CONFIG}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    log_success "Service xray-xhttp-nginx created"
}

create_nginx_config() {
    local domain="$1"
    local xhttp_path="$2"

    log_info "Creating Nginx configuration..."

    cat > "${NGINX_SITE}" << EOF
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name ${domain};

    ssl_certificate /etc/letsencrypt/live/${domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    # Decoy website
    location / {
        root ${WEBSITE_DIR};
        index index.html;
        try_files \$uri \$uri/ =404;
    }

    # XHTTP proxy to Xray via Unix socket
    location ${xhttp_path} {
        grpc_pass unix:${XRAY_SOCKET};
        grpc_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        grpc_set_header X-Real-IP \$remote_addr;
        grpc_read_timeout 315s;
        grpc_send_timeout 300s;
        grpc_connect_timeout 5s;
        client_max_body_size 0;
    }

    location /.well-known/acme-challenge/ {
        root ${WEBSITE_DIR};
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name ${domain};
    location /.well-known/acme-challenge/ { root ${WEBSITE_DIR}; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF

    ln -sf "${NGINX_SITE}" "${NGINX_SITE_ENABLED}"
    rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true

    nginx -t || { log_error "Nginx config test failed"; exit 1; }
    systemctl reload nginx
    log_success "Nginx configured for ${domain}"
}

create_decoy_website() {
    log_info "Creating decoy website..."
    mkdir -p "${WEBSITE_DIR}"

    cat > "${WEBSITE_DIR}/index.html" << 'HTMLEOF'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GameLogs.io - Real-Time Game Server Log Aggregation</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #0d1117;
    color: #c9d1d9;
    line-height: 1.6;
  }
  a { color: #58a6ff; text-decoration: none; }
  a:hover { text-decoration: underline; }

  header {
    border-bottom: 1px solid #21262d;
    padding: 16px 0;
  }
  .container { max-width: 1100px; margin: 0 auto; padding: 0 24px; }
  nav { display: flex; align-items: center; justify-content: space-between; }
  .logo { font-size: 1.25rem; font-weight: 700; color: #f0f6fc; letter-spacing: -0.5px; }
  .logo span { color: #58a6ff; }
  .nav-links { display: flex; gap: 24px; font-size: 0.875rem; color: #8b949e; }
  .nav-links a { color: #8b949e; }
  .nav-links a:hover { color: #c9d1d9; }
  .btn {
    display: inline-block;
    padding: 8px 18px;
    border-radius: 6px;
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
    border: none;
  }
  .btn-primary { background: #238636; color: #fff; }
  .btn-primary:hover { background: #2ea043; text-decoration: none; }
  .btn-outline { background: transparent; color: #c9d1d9; border: 1px solid #30363d; }
  .btn-outline:hover { background: #21262d; text-decoration: none; }

  .hero {
    padding: 72px 0 56px;
    text-align: center;
  }
  .hero h1 {
    font-size: 2.8rem;
    font-weight: 800;
    color: #f0f6fc;
    letter-spacing: -1px;
    margin-bottom: 16px;
  }
  .hero p {
    font-size: 1.125rem;
    color: #8b949e;
    max-width: 560px;
    margin: 0 auto 32px;
  }
  .hero-cta { display: flex; gap: 12px; justify-content: center; }

  .stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    background: #21262d;
    border: 1px solid #21262d;
    border-radius: 8px;
    overflow: hidden;
    margin: 48px 0;
  }
  .stat {
    background: #0d1117;
    padding: 28px 24px;
    text-align: center;
  }
  .stat-value { font-size: 1.75rem; font-weight: 700; color: #f0f6fc; }
  .stat-label { font-size: 0.8rem; color: #8b949e; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.05em; }

  .features {
    padding: 48px 0;
  }
  .features h2 {
    font-size: 1.5rem;
    font-weight: 700;
    color: #f0f6fc;
    margin-bottom: 32px;
    text-align: center;
  }
  .feature-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
  }
  .feature-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 24px;
  }
  .feature-icon {
    font-size: 1.5rem;
    margin-bottom: 12px;
  }
  .feature-card h3 {
    font-size: 0.9375rem;
    font-weight: 600;
    color: #f0f6fc;
    margin-bottom: 8px;
  }
  .feature-card p {
    font-size: 0.8125rem;
    color: #8b949e;
    line-height: 1.5;
  }

  .warning-box {
    background: #2d2200;
    border: 1px solid #7d5a00;
    border-radius: 8px;
    padding: 20px 24px;
    margin: 40px 0;
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }
  .warning-icon { font-size: 1.125rem; flex-shrink: 0; margin-top: 2px; }
  .warning-box p { font-size: 0.875rem; color: #d4a72c; line-height: 1.6; }
  .warning-box strong { color: #e3b341; }

  .quickstart {
    padding: 48px 0;
  }
  .quickstart h2 {
    font-size: 1.5rem;
    font-weight: 700;
    color: #f0f6fc;
    margin-bottom: 8px;
  }
  .quickstart p {
    color: #8b949e;
    margin-bottom: 20px;
    font-size: 0.9rem;
  }
  .code-block {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 20px 24px;
    font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
    font-size: 0.8125rem;
    line-height: 1.8;
    overflow-x: auto;
  }
  .code-block .comment { color: #8b949e; }
  .code-block .cmd { color: #79c0ff; }
  .code-block .str { color: #a5d6ff; }
  .code-block .var { color: #ff7b72; }

  .cta-section {
    text-align: center;
    padding: 56px 0;
    border-top: 1px solid #21262d;
  }
  .cta-section h2 {
    font-size: 1.75rem;
    font-weight: 700;
    color: #f0f6fc;
    margin-bottom: 12px;
  }
  .cta-section p { color: #8b949e; margin-bottom: 24px; }
  .contact-email {
    font-family: monospace;
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 6px;
    padding: 6px 14px;
    font-size: 0.9rem;
    color: #58a6ff;
  }

  footer {
    border-top: 1px solid #21262d;
    padding: 24px 0;
    text-align: center;
    font-size: 0.8rem;
    color: #484f58;
  }

  @media (max-width: 768px) {
    .hero h1 { font-size: 2rem; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    .feature-grid { grid-template-columns: 1fr; }
    .nav-links { display: none; }
  }
</style>
</head>
<body>

<header>
  <div class="container">
    <nav>
      <div class="logo">Game<span>Logs</span>.io</div>
      <div class="nav-links">
        <a href="#">Docs</a>
        <a href="#">Pricing</a>
        <a href="#">Status</a>
        <a href="#">Blog</a>
      </div>
      <div style="display:flex;gap:8px">
        <a href="#" class="btn btn-outline">Sign in</a>
        <a href="#" class="btn btn-primary">Get started</a>
      </div>
    </nav>
  </div>
</header>

<main>
  <div class="container">

    <section class="hero">
      <h1>Real-Time Game Server<br>Log Aggregation</h1>
      <p>Collect, index, and analyze logs from every game server instance in your fleet. Sub-50ms ingestion latency, full-text search, and live alerting - built for scale.</p>
      <div class="hero-cta">
        <a href="#" class="btn btn-primary">Start free trial</a>
        <a href="#" class="btn btn-outline">View documentation</a>
      </div>
    </section>

    <div class="stats">
      <div class="stat">
        <div class="stat-value">2.4B+</div>
        <div class="stat-label">Events daily</div>
      </div>
      <div class="stat">
        <div class="stat-value">340+</div>
        <div class="stat-label">Active servers</div>
      </div>
      <div class="stat">
        <div class="stat-value">&lt;50ms</div>
        <div class="stat-label">Ingestion latency</div>
      </div>
      <div class="stat">
        <div class="stat-value">99.97%</div>
        <div class="stat-label">Uptime SLA</div>
      </div>
    </div>

    <section class="features">
      <h2>Everything you need for game server observability</h2>
      <div class="feature-grid">
        <div class="feature-card">
          <div class="feature-icon">&#9889;</div>
          <h3>Streaming Ingestion</h3>
          <p>Push logs in real time via our lightweight agent or HTTP endpoint. Handles burst spikes from player events, crashes, and match starts without dropping events.</p>
        </div>
        <div class="feature-card">
          <div class="feature-icon">&#128276;</div>
          <h3>Smart Alerting</h3>
          <p>Define threshold and anomaly-based alerts. Get notified via Slack, PagerDuty, or webhook when crash rates spike or when specific error patterns appear.</p>
        </div>
        <div class="feature-card">
          <div class="feature-icon">&#128197;</div>
          <h3>Retention Policies</h3>
          <p>Configure per-stream retention from 7 days to 2 years. Hot storage for recent logs, cold storage for compliance and post-mortems.</p>
        </div>
        <div class="feature-card">
          <div class="feature-icon">&#128202;</div>
          <h3>Live Dashboard</h3>
          <p>Pre-built dashboards for player sessions, server TPS, match events, and error rates. Build custom panels with our drag-and-drop editor.</p>
        </div>
        <div class="feature-card">
          <div class="feature-icon">&#128269;</div>
          <h3>Full-Text Search</h3>
          <p>Search across billions of log lines in milliseconds. Filter by server region, game mode, build version, or any custom field in your structured logs.</p>
        </div>
        <div class="feature-card">
          <div class="feature-icon">&#128279;</div>
          <h3>REST &amp; gRPC API</h3>
          <p>Integrate with your existing observability stack. Ingest via REST, gRPC streaming, or Kafka-compatible endpoint. Export to S3, BigQuery, or Elasticsearch.</p>
        </div>
      </div>
    </section>

    <div class="warning-box">
      <div class="warning-icon">&#9888;</div>
      <p><strong>Bandwidth notice:</strong> Real-time log streaming from active game servers can consume several hundred gigabytes of traffic per day depending on your fleet size and log verbosity level. Review your ingestion volume estimate in the onboarding wizard before enabling streaming on production servers.</p>
    </div>

    <section class="quickstart">
      <h2>Quick start</h2>
      <p>Install the GameLogs agent on your game server in under two minutes.</p>
      <div class="code-block">
        <span class="comment"># Install agent</span><br>
        <span class="cmd">curl</span> -fsSL https://get.gamelogs.io/agent | <span class="cmd">bash</span><br>
        <br>
        <span class="comment"># Configure endpoint</span><br>
        <span class="var">export</span> <span class="str">GAMELOGS_ENDPOINT</span>=<span class="str">"https://logs.soulfly.us/ingest/v2/"</span><br>
        <span class="var">export</span> <span class="str">GAMELOGS_API_KEY</span>=<span class="str">"gl_your_api_key_here"</span><br>
        <br>
        <span class="comment"># Start streaming</span><br>
        <span class="cmd">gamelogs-agent</span> start --follow <span class="str">/var/log/gameserver/*.log</span><br>
        <br>
        <span class="comment"># Verify connectivity</span><br>
        <span class="cmd">gamelogs-agent</span> status
      </div>
    </section>

    <section class="cta-section">
      <h2>Ready to get started?</h2>
      <p>Free tier includes up to 5 servers and 500M events per month. No credit card required.</p>
      <a href="#" class="btn btn-primary" style="font-size:1rem;padding:12px 28px;margin-bottom:24px;display:inline-block;">Create free account</a>
      <br>
      <p style="font-size:0.875rem;color:#8b949e;margin-bottom:8px;">Questions or enterprise pricing?</p>
      <span class="contact-email">gamelogs@soulfly.us</span>
    </section>

  </div>
</main>

<footer>
  <div class="container">
    &copy; 2024-2026 GameLogs.io. Operated by Soulfly Interactive LLC.
    &nbsp;&middot;&nbsp; <a href="#">Privacy</a>
    &nbsp;&middot;&nbsp; <a href="#">Terms</a>
    &nbsp;&middot;&nbsp; <a href="#">Status</a>
  </div>
</footer>

</body>
</html>
HTMLEOF

    chmod -R 755 "${WEBSITE_DIR}"
    log_success "Decoy website created at ${WEBSITE_DIR}"
}

configure_server() {
    echo ""
    echo -e "${GREEN}=== XRay XHTTP+Nginx Configuration ===${NC}"
    echo ""

    mkdir -p "${XHTTP_NGINX_DIR}" "${CLIENT_DIR}"
    chmod 700 "${XHTTP_NGINX_DIR}" "${CLIENT_DIR}"

    # Domain
    echo -e "${CYAN}Domain (A record must already point to this server):${NC}"
    echo -e "  Example: logs.soulfly.us"
    read -rp "Domain: " DOMAIN
    [[ -z "${DOMAIN}" ]] && { log_error "Domain required"; exit 1; }

    # XHTTP path
    echo ""
    echo -e "${CYAN}XHTTP ingest path:${NC}"
    read -rp "Path [${DEFAULT_XHTTP_PATH}]: " XHTTP_PATH
    XHTTP_PATH=${XHTTP_PATH:-${DEFAULT_XHTTP_PATH}}
    [[ "${XHTTP_PATH}" != */ ]] && XHTTP_PATH="${XHTTP_PATH}/"
    [[ "${XHTTP_PATH}" != /* ]] && XHTTP_PATH="/${XHTTP_PATH}"

    # TLS fingerprint
    echo ""
    echo -e "${CYAN}Client TLS fingerprint [${DEFAULT_FINGERPRINT}]:${NC}"
    echo -e "  Options: chrome, firefox, safari, ios, android, edge, 360, qq, random, randomized"
    read -rp "Fingerprint [${DEFAULT_FINGERPRINT}]: " XHTTP_FINGERPRINT
    XHTTP_FINGERPRINT=${XHTTP_FINGERPRINT:-${DEFAULT_FINGERPRINT}}

    # Generate VLESS Encryption keys
    log_info "Generating VLESS Encryption (vlessenc) keys..."
    local vlessenc_output
    vlessenc_output=$(xray vlessenc 2>&1)
    VLESSENC_DECRYPTION=$(echo "${vlessenc_output}" | grep '"decryption"' | head -1 | sed 's/.*: "//;s/"//')
    VLESSENC_ENCRYPTION=$(echo "${vlessenc_output}" | grep '"encryption"' | head -1 | sed 's/.*: "//;s/"//')
    FLOW="xtls-rprx-vision"

    if [[ -z "${VLESSENC_DECRYPTION}" || -z "${VLESSENC_ENCRYPTION}" ]]; then
        log_warn "Failed to generate vlessenc keys - falling back to unencrypted mode"
        VLESSENC_DECRYPTION="none"
        VLESSENC_ENCRYPTION="none"
        FLOW=""
    else
        log_success "VLESS Encryption keys generated (X25519 authentication)"
    fi

    # Save params
    cat > "${XHTTP_NGINX_PARAMS}" << EOF
DOMAIN='${DOMAIN}'
XHTTP_PATH='${XHTTP_PATH}'
XHTTP_FINGERPRINT='${XHTTP_FINGERPRINT}'
VLESSENC_DECRYPTION='${VLESSENC_DECRYPTION}'
VLESSENC_ENCRYPTION='${VLESSENC_ENCRYPTION}'
FLOW='${FLOW}'
EOF
    chmod 600 "${XHTTP_NGINX_PARAMS}"

    log_success "Server parameters saved"
}

create_xhttp_nginx_config() {
    local clients_json="[]"

    if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        clients_json="["
        local first=true
        local client_uuid

        for client_file in ${CLIENT_DIR}/*.conf; do
            client_uuid=$(grep "^CLIENT_UUID=" "$client_file" | cut -d= -f2 | tr -d '"')

            if [[ "$first" == "true" ]]; then
                first=false
            else
                clients_json+=","
            fi
            if [[ -n "${FLOW}" ]]; then
                clients_json+="{\"id\":\"${client_uuid}\",\"flow\":\"${FLOW}\"}"
            else
                clients_json+="{\"id\":\"${client_uuid}\"}"
            fi
        done

        clients_json+="]"
    fi

    source "${XHTTP_NGINX_PARAMS}"

    cat > "${XHTTP_NGINX_CONFIG}" << EOF
{
    "log": {
        "loglevel": "warning"
    },
    "inbounds": [
        {
            "listen": "${XRAY_SOCKET},0666",
            "protocol": "vless",
            "settings": {
                "clients": ${clients_json},
                "decryption": "${VLESSENC_DECRYPTION}"
            },
            "streamSettings": {
                "network": "xhttp",
                "security": "none",
                "xhttpSettings": {
                    "mode": "auto",
                    "path": "${XHTTP_PATH}"
                }
            },
            "sniffing": {
                "enabled": true,
                "destOverride": ["http", "tls", "quic"]
            }
        }
    ],
    "outbounds": [
        {
            "protocol": "freedom",
            "tag": "direct"
        },
        {
            "protocol": "blackhole",
            "tag": "block"
        }
    ]
}
EOF
    chmod 600 "${XHTTP_NGINX_CONFIG}"
}

start_xhttp_nginx() {
    log_info "Starting XRay XHTTP+Nginx..."
    systemctl daemon-reload
    systemctl enable xray-xhttp-nginx
    systemctl restart xray-xhttp-nginx
    sleep 2
    service_is_active xray-xhttp-nginx && log_success "XRay XHTTP+Nginx running" || {
        log_error "Failed to start xray-xhttp-nginx"
        journalctl -u xray-xhttp-nginx --no-pager -n 20
        exit 1
    }
}

run_install() {
    check_root
    check_os
    install_essentials
    install_xray_binary
    install_nginx
    create_xhttp_nginx_service
    configure_server

    if [[ -f "${XHTTP_NGINX_PARAMS}" ]]; then
        source "${XHTTP_NGINX_PARAMS}"
    else
        log_error "Params file not found: ${XHTTP_NGINX_PARAMS}"
        exit 1
    fi

    create_decoy_website
    setup_letsencrypt "${DOMAIN}"
    create_nginx_config "${DOMAIN}" "${XHTTP_PATH}"
    create_xhttp_nginx_config
    start_xhttp_nginx

    # Open firewall ports
    firewall_open_port 80 tcp
    firewall_open_port 443 tcp

    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  XRay XHTTP+Nginx Installation Complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
    echo -e "Domain:     ${CYAN}${DOMAIN}${NC}"
    echo -e "Transport:  ${CYAN}XHTTP (mode: auto)${NC}"
    echo -e "Path:       ${CYAN}${XHTTP_PATH}${NC}"
    echo -e "TLS:        ${CYAN}Let's Encrypt (via Nginx)${NC}"
    echo -e "Socket:     ${CYAN}${XRAY_SOCKET}${NC}"
    echo ""
    echo -e "${YELLOW}Now add a client to connect:${NC}"
    echo ""

    create_client
}


# ============================================================================
# CLIENT MANAGEMENT
# ============================================================================

create_client() {
    load_params

    echo ""
    read -rp "Client name: " input_name
    [[ -z "${input_name}" ]] && { log_error "Name required"; return 1; }

    local new_client_name new_client_file new_client_uuid new_created_date
    new_client_name=$(sanitize_client_name "${input_name}")
    new_client_file="${CLIENT_DIR}/${new_client_name}.conf"

    [[ -f "${new_client_file}" ]] && { log_error "Client '${new_client_name}' already exists"; return 1; }

    new_client_uuid=$(xray uuid)
    new_created_date=$(date +%Y-%m-%d)

    cat > "${new_client_file}" << EOF
CLIENT_NAME="${new_client_name}"
CLIENT_UUID="${new_client_uuid}"
CREATED_DATE="${new_created_date}"
EOF
    chmod 600 "${new_client_file}"

    create_xhttp_nginx_config
    systemctl restart xray-xhttp-nginx

    log_success "Client '${new_client_name}' created"
    show_client_config "${new_client_name}"
}

show_client_config() {
    local name="$1"
    if [[ -z "$name" ]]; then
        if ! select_client "Show config for client"; then
            return
        fi
        name="${SELECTED_CLIENT_NAME}"
    fi

    local client_file="${CLIENT_DIR}/${name}.conf"
    [[ ! -f "${client_file}" ]] && { log_error "Client '${name}' not found"; return 1; }

    source "${XHTTP_NGINX_PARAMS}"

    local CLIENT_UUID CLIENT_NAME CREATED_DATE
    source "${client_file}"
    CLIENT_NAME="${name}"

    local ENCODED_PATH
    ENCODED_PATH=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${XHTTP_PATH}', safe=''))" 2>/dev/null || \
                   echo "${XHTTP_PATH}" | sed 's|/|%2F|g')

    local PROFILE_NAME="${DOMAIN}-${CLIENT_NAME}"
    local enc_param="none"
    local flow_param=""
    [[ -n "${VLESSENC_ENCRYPTION}" && "${VLESSENC_ENCRYPTION}" != "none" ]] && enc_param="${VLESSENC_ENCRYPTION}"
    [[ -n "${FLOW}" ]] && flow_param="&flow=${FLOW}"
    local VLESS_URL="vless://${CLIENT_UUID}@${DOMAIN}:443?encryption=${enc_param}&security=tls&sni=${DOMAIN}&type=xhttp&path=${ENCODED_PATH}&mode=stream-up&fp=${XHTTP_FINGERPRINT}${flow_param}#${PROFILE_NAME}"

    echo ""
    echo -e "${GREEN}=== Client: ${CLIENT_NAME} ===${NC}"
    echo ""
    echo -e "Domain:      ${CYAN}${DOMAIN}${NC}"
    echo -e "Port:        ${CYAN}443 (Nginx + Let's Encrypt)${NC}"
    echo -e "UUID:        ${CYAN}${CLIENT_UUID}${NC}"
    echo -e "Transport:   ${CYAN}XHTTP (mode: stream-up)${NC}"
    echo -e "Security:    ${CYAN}tls${NC}"
    echo -e "Path:        ${CYAN}${XHTTP_PATH}${NC}"
    echo -e "Fingerprint: ${CYAN}${XHTTP_FINGERPRINT}${NC}"
    if [[ -n "${FLOW}" ]]; then
        echo -e "Flow:        ${CYAN}${FLOW}${NC}"
    fi
    if [[ -n "${VLESSENC_ENCRYPTION}" && "${VLESSENC_ENCRYPTION}" != "none" ]]; then
        echo -e "Encryption:  ${CYAN}VLESS Encryption (X25519)${NC}"
    fi
    echo -e "Created:     ${CYAN}${CREATED_DATE}${NC}"
    echo ""
    echo -e "${GREEN}VLESS URL (copy to v2rayNG / NekoBox / Hiddify / v2rayN):${NC}"
    echo -e "${CYAN}${VLESS_URL}${NC}"
    echo ""

    echo "${VLESS_URL}" > "${CLIENT_DIR}/${CLIENT_NAME}-vless.txt"

    if command -v qrencode &>/dev/null; then
        echo -e "${GREEN}QR Code:${NC}"
        echo "${VLESS_URL}" | qrencode -t ansiutf8
    fi

    local user_enc="${VLESSENC_ENCRYPTION:-none}"
    local user_flow_line=""
    [[ -n "${FLOW}" ]] && user_flow_line=",
                                \"flow\": \"${FLOW}\""

    cat > "${CLIENT_DIR}/${CLIENT_NAME}-config.json" << EOF
{
    "outbounds": [
        {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "${DOMAIN}",
                        "port": 443,
                        "users": [
                            {
                                "id": "${CLIENT_UUID}",
                                "encryption": "${user_enc}"${user_flow_line}
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {
                "network": "xhttp",
                "security": "tls",
                "tlsSettings": {
                    "serverName": "${DOMAIN}",
                    "fingerprint": "${XHTTP_FINGERPRINT}"
                },
                "xhttpSettings": {
                    "mode": "stream-up",
                    "path": "${XHTTP_PATH}"
                }
            }
        }
    ]
}
EOF
}

count_clients() {
    local n=0
    if [[ -d ${CLIENT_DIR} ]] && ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        n=$(ls ${CLIENT_DIR}/*.conf 2>/dev/null | wc -l)
    fi
    echo "${n}"
}

list_clients() {
    load_params

    echo ""
    echo -e "${GREEN}=== XHTTP+Nginx Clients ===${NC}"
    echo ""

    if [[ ! -d ${CLIENT_DIR} ]] || ! ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        echo -e "  ${YELLOW}No clients configured${NC}"
        return
    fi

    printf "  %-4s %-20s %-12s\n" "#" "NAME" "CREATED"
    printf "  %-4s %-20s %-12s\n" "----" "--------------------" "------------"

    local i=1 client_name created_date
    for client_file in ${CLIENT_DIR}/*.conf; do
        client_name=$(grep "^CLIENT_NAME=" "$client_file" | cut -d= -f2 | tr -d '"')
        created_date=$(grep "^CREATED_DATE=" "$client_file" | cut -d= -f2 | tr -d '"')
        printf "  %-4s %-20s %-12s\n" "${i})" "${client_name}" "${created_date}"
        i=$((i + 1))
    done
    echo ""
}

# Select a client by number from the list. Sets SELECTED_CLIENT_NAME.
select_client() {
    local prompt="${1:-Select client}"
    load_params

    if [[ ! -d ${CLIENT_DIR} ]] || ! ls ${CLIENT_DIR}/*.conf &>/dev/null 2>&1; then
        echo -e "  ${YELLOW}No clients configured${NC}"
        return 1
    fi

    list_clients

    local clients=()
    for client_file in ${CLIENT_DIR}/*.conf; do
        clients+=("$(grep "^CLIENT_NAME=" "$client_file" | cut -d= -f2 | tr -d '"')")
    done

    local selection
    read -rp "${prompt} [1-${#clients[@]}]: " selection
    [[ -z "${selection}" ]] && return 1

    if ! [[ "${selection}" =~ ^[0-9]+$ ]] || (( selection < 1 || selection > ${#clients[@]} )); then
        log_error "Invalid selection"
        return 1
    fi

    SELECTED_CLIENT_NAME="${clients[$((selection - 1))]}"
    return 0
}

revoke_client() {
    if ! select_client "Client to revoke"; then
        return
    fi

    local revoke_name="${SELECTED_CLIENT_NAME}"
    local revoke_file="${CLIENT_DIR}/${revoke_name}.conf"

    confirm_action "Revoke client '${revoke_name}'?" || return

    rm -f "${revoke_file}"
    rm -f "${CLIENT_DIR}/${revoke_name}-vless.txt"
    rm -f "${CLIENT_DIR}/${revoke_name}-config.json"

    create_xhttp_nginx_config
    systemctl restart xray-xhttp-nginx

    log_success "Client '${revoke_name}' revoked"
}


# ============================================================================
# MANAGEMENT FUNCTIONS
# ============================================================================

load_params() {
    [[ ! -f "${XHTTP_NGINX_PARAMS}" ]] && { log_error "XHTTP+Nginx not installed"; exit 1; }
    source "${XHTTP_NGINX_PARAMS}"
}

show_status() {
    echo ""
    echo -e "${GREEN}=== XRay XHTTP+Nginx Status ===${NC}"
    echo ""

    echo -e "${CYAN}--- xray-xhttp-nginx service ---${NC}"
    systemctl status xray-xhttp-nginx --no-pager 2>/dev/null | head -10 || echo -e "  ${RED}Not installed${NC}"

    echo ""
    echo -e "${CYAN}--- nginx service ---${NC}"
    systemctl status nginx --no-pager 2>/dev/null | head -10 || echo -e "  ${RED}Not installed${NC}"

    echo ""
    if [[ -S "${XRAY_SOCKET}" ]]; then
        echo -e "Unix socket:    ${GREEN}${XRAY_SOCKET} (exists)${NC}"
    else
        echo -e "Unix socket:    ${RED}${XRAY_SOCKET} (missing)${NC}"
    fi

    echo -e "Active clients: ${CYAN}$(count_clients)${NC}"

    if [[ -f "${XHTTP_NGINX_PARAMS}" ]]; then
        source "${XHTTP_NGINX_PARAMS}"
        echo -e "Domain:         ${CYAN}${DOMAIN}${NC}"
        echo -e "XHTTP path:     ${CYAN}${XHTTP_PATH}${NC}"

        local cert_file="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
        if [[ -f "${cert_file}" ]]; then
            local expiry
            expiry=$(openssl x509 -enddate -noout -in "${cert_file}" 2>/dev/null | cut -d= -f2)
            echo -e "TLS cert:       ${CYAN}expires ${expiry}${NC}"
        fi
    fi
}

manual_update() {
    log_info "Checking for XRay updates..."

    local current
    current=$(xray version 2>/dev/null | head -1 | awk '{print $2}')
    echo -e "Current version: ${CYAN}${current}${NC}"

    local latest
    latest=$(curl -sL "https://api.github.com/repos/XTLS/Xray-core/releases/latest" 2>/dev/null | grep '"tag_name"' | head -1 | cut -d'"' -f4)
    latest="${latest#v}"

    if [[ -n "${latest}" ]]; then
        echo -e "Latest version:  ${CYAN}${latest}${NC}"
        if [[ "${current}" == "${latest}" ]]; then
            log_success "Already running the latest version"
            press_enter
            return 0
        fi
        echo -e "${YELLOW}Update available: ${current} -> ${latest}${NC}"
    else
        echo -e "Latest version:  ${YELLOW}(couldn't fetch from GitHub)${NC}"
    fi

    echo ""
    if ! confirm_action "Run update now?"; then
        return 0
    fi

    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install

    local new
    new=$(xray version 2>/dev/null | head -1 | awk '{print $2}')

    if [[ "${current}" != "${new}" ]]; then
        log_success "Updated to ${new}"
        systemctl restart xray-xhttp-nginx
        if service_is_active xray 2>/dev/null; then
            log_warning "xray.service (VLESS+REALITY) is also running - consider restarting it too"
        fi
        if service_is_active xray-xhttp 2>/dev/null; then
            log_warning "xray-xhttp.service (CDN Tunnel) is also running - consider restarting it too"
        fi
    else
        log_info "Already at latest version"
    fi

    press_enter
}

uninstall_xhttp_nginx() {
    log_warning "This will remove XHTTP+Nginx configuration, Nginx site, and client data!"
    log_warning "The XRay binary and Let's Encrypt certificate will NOT be removed."
    confirm_action "Continue?" || return

    systemctl stop xray-xhttp-nginx 2>/dev/null || true
    systemctl disable xray-xhttp-nginx 2>/dev/null || true
    rm -f /etc/systemd/system/xray-xhttp-nginx.service

    rm -f "${NGINX_SITE}" "${NGINX_SITE_ENABLED}"
    nginx -t 2>/dev/null && systemctl reload nginx 2>/dev/null || true

    rm -rf "${XHTTP_NGINX_DIR}" /etc/vpn/xhttp-nginx "${WEBSITE_DIR}"

    systemctl daemon-reload
    log_success "XHTTP+Nginx uninstalled (XRay binary and TLS certificate preserved)"
}


# ============================================================================
# MAIN MENU
# ============================================================================

main_menu() {
    check_root
    check_os

    while true; do
        echo ""
        echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
        echo -e "${GREEN}║     XRay XHTTP + Nginx                 ║${NC}"
        echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
        echo ""

        if [[ -f ${XHTTP_NGINX_PARAMS} ]]; then
            source "${XHTTP_NGINX_PARAMS}"
            service_is_active xray-xhttp-nginx && echo -e "  XRay XHTTP: ${GREEN}Running${NC}" || echo -e "  XRay XHTTP: ${RED}Stopped${NC}"
            service_is_active nginx            && echo -e "  Nginx:      ${GREEN}Running${NC}" || echo -e "  Nginx:      ${RED}Stopped${NC}"

            echo -e "  Clients:    ${CYAN}$(count_clients)${NC}"
            echo -e "  Domain:     ${CYAN}${DOMAIN}${NC}"
            echo -e "  Transport:  ${CYAN}XHTTP (stream-up, Let's Encrypt TLS)${NC}"
            echo ""
            echo "  1) Add client"
            echo "  2) List clients"
            echo "  3) Show client config & QR"
            echo "  4) Revoke client"
            echo "  5) Show status"
            echo "  6) Restart services"
            echo "  7) Update XRay"
            echo "  8) Uninstall"
        else
            echo -e "  Status: ${YELLOW}Not installed${NC}"
            echo ""
            echo -e "  ${CYAN}Prerequisites (before installing):${NC}"
            echo "    1. Point your domain's A record to this server's public IP"
            echo "    2. Ensure ports 80 and 443 are open in your firewall"
            echo "    3. Run install - Nginx and Let's Encrypt will be set up automatically"
            echo ""
            echo "  1) Install XRay XHTTP+Nginx"
        fi
        echo ""
        echo "  0) Exit"
        echo ""
        read -rp "Select: " choice

        if [[ -f ${XHTTP_NGINX_PARAMS} ]]; then
            case ${choice} in
                1) create_client ;;
                2) list_clients ;;
                3)
                    list_clients
                    show_client_config
                    ;;
                4) revoke_client ;;
                5) show_status ;;
                6)
                    systemctl restart xray-xhttp-nginx 2>/dev/null && log_success "xray-xhttp-nginx restarted" || log_error "Failed to restart xray-xhttp-nginx"
                    systemctl reload nginx 2>/dev/null && log_success "nginx reloaded" || log_error "Failed to reload nginx"
                    ;;
                7) manual_update ;;
                8) uninstall_xhttp_nginx ;;
                0) exit 0 ;;
            esac
        else
            case ${choice} in
                1) run_install ;;
                0) exit 0 ;;
            esac
        fi
    done
}

main_menu
