#!/bin/sh
# Installation script for Xray Health Monitor LuCI Status Widget
# This script installs all necessary components to display xray health status on OpenWRT LuCI interface
#
# USAGE:
#   ./install-xray-health-widget.sh install     - Install all components
#   ./install-xray-health-widget.sh uninstall   - Remove all components
#
# COMPONENTS INSTALLED:
#   - LuCI status widget (JavaScript)
#   - RPC backend handler (Shell script)
#   - RPC ACL permissions
#   - Init.d service script
#   - Sysupgrade.conf entries (for firmware update persistence)

set -e

###############################################################################
# CONFIGURATION
###############################################################################

WIDGET_PATH="/www/luci-static/resources/view/status/include/00_xray_health.js"
RPC_PATH="/usr/libexec/rpcd/xray-health"
ACL_PATH="/usr/share/rpcd/acl.d/luci-app-xray-health.json"
INITD_PATH="/etc/init.d/xray-health"
DAEMON_PATH="/usr/local/sbin/check-xray.sh"
SYSUPGRADE_CONF="/etc/sysupgrade.conf"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

###############################################################################
# UTILITY FUNCTIONS
###############################################################################

log_info() {
    printf '%b[INFO]%b %s\n' "${GREEN}" "${NC}" "$*"
}

log_warn() {
    printf '%b[WARN]%b %s\n' "${YELLOW}" "${NC}" "$*"
}

log_error() {
    printf '%b[ERROR]%b %s\n' "${RED}" "${NC}" "$*"
}

die() {
    log_error "$*"
    exit 1
}

###############################################################################
# SYSUPGRADE.CONF MANAGEMENT
###############################################################################

add_to_sysupgrade_conf() {
    local entry="$1"
    
    # Check if entry already exists
    if grep -Fxq "$entry" "$SYSUPGRADE_CONF" 2>/dev/null; then
        return 0  # Already exists, skip
    fi
    
    # Add entry
    echo "$entry" >> "$SYSUPGRADE_CONF"
    return 1  # Was added
}

update_sysupgrade_conf() {
    log_info "Updating sysupgrade.conf for firmware update persistence..."
    
    if [ ! -f "$SYSUPGRADE_CONF" ]; then
        log_warn "Creating $SYSUPGRADE_CONF"
        cat > "$SYSUPGRADE_CONF" <<'EOF'
## This file contains files and directories that should
## be preserved during an upgrade.

EOF
    fi
    
    local added_count=0
    
    # Add xray-health specific entries
    if add_to_sysupgrade_conf "$DAEMON_PATH"; then
        log_info "  Added: $DAEMON_PATH"
        added_count=$((added_count + 1))
    fi
    
    if add_to_sysupgrade_conf "/etc/xray-health.state"; then
        log_info "  Added: /etc/xray-health.state"
        added_count=$((added_count + 1))
    fi
    
    if add_to_sysupgrade_conf "/etc/xray-health-logrotate"; then
        log_info "  Added: /etc/xray-health-logrotate"
        added_count=$((added_count + 1))
    fi
    
    if add_to_sysupgrade_conf "/root/xray-health-persistent.log*"; then
        log_info "  Added: /root/xray-health-persistent.log*"
        added_count=$((added_count + 1))
    fi
    
    # Add widget and RPC backend
    if add_to_sysupgrade_conf "$WIDGET_PATH"; then
        log_info "  Added: $WIDGET_PATH"
        added_count=$((added_count + 1))
    fi
    
    if add_to_sysupgrade_conf "$RPC_PATH"; then
        log_info "  Added: $RPC_PATH"
        added_count=$((added_count + 1))
    fi
    
    if add_to_sysupgrade_conf "$ACL_PATH"; then
        log_info "  Added: $ACL_PATH"
        added_count=$((added_count + 1))
    fi
    
    if add_to_sysupgrade_conf "$INITD_PATH"; then
        log_info "  Added: $INITD_PATH"
        added_count=$((added_count + 1))
    fi
    
    if [ $added_count -eq 0 ]; then
        log_info "  All entries already present in $SYSUPGRADE_CONF"
    else
        log_info "  Added $added_count new entries to $SYSUPGRADE_CONF"
    fi
}

###############################################################################
# INSTALL FUNCTIONS
###############################################################################

install_widget() {
    log_info "Installing LuCI status widget..."
    
    local widget_dir
    widget_dir="$(dirname "$WIDGET_PATH")"
    mkdir -p "$widget_dir"
    
    cat > "$WIDGET_PATH" <<'WIDGET_EOF'
'use strict';
'require baseclass';
'require rpc';

var callXrayHealthStatus = rpc.declare({
	object: 'xray-health',
	method: 'getStatus',
	expect: { '': {} }
});

return baseclass.extend({
	title: 'Xray Tunnel',

	load: function() {
		return callXrayHealthStatus();
	},

	render: function(data) {
		if (!data || !data.instances || data.instances.length < 2) {
			return E('div', {
				'class': 'cbi-section',
				'style': 'margin-bottom:1em'
			}, E('span', {
				'class': 'xray-health-label xray-health-undefined',
				'title': 'Xray health status unknown'
			}, _('Xray: Undefined')));
		}

		var xrayInstance = data.instances[0];
		var inetInstance = data.instances[1];
		
		// Xray Tunnel Status
		var xrayStatus = _('Unknown');
		var xrayClassName = 'xray-health-label xray-health-undefined';
		var xrayTitle = 'Xray tunnel status unknown';

		if (xrayInstance.inet === 0) {
			xrayStatus = _('Tunnel Active');
			xrayClassName = 'xray-health-label xray-health-connected';
			xrayTitle = 'Xray tunnel is active and healthy';
		} else if (xrayInstance.inet === 1) {
			xrayStatus = _('Tunnel Down');
			xrayClassName = 'xray-health-label xray-health-disconnected';
			xrayTitle = 'Tunnel down - using direct internet';
		} else if (xrayInstance.inet === -1) {
			xrayStatus = _('Checking...');
			xrayClassName = 'xray-health-label xray-health-checking';
			xrayTitle = 'Checking tunnel connectivity';
		}

		if (xrayInstance.status) {
			xrayTitle += ' (' + xrayInstance.status + ')';
		}
		
		// Internet Status
		var inetStatus = _('Unknown');
		var inetClassName = 'xray-health-label xray-health-undefined';
		var inetTitle = 'Internet status unknown';

		if (inetInstance.inet === 0) {
			inetStatus = _('Connected');
			inetClassName = 'xray-health-label xray-health-connected';
			inetTitle = 'ISP internet is connected';
		} else if (inetInstance.inet === 1) {
			inetStatus = _('Disconnected');
			inetClassName = 'xray-health-label xray-health-disconnected';
			inetTitle = 'ISP internet is down';
		} else if (inetInstance.inet === -1) {
			inetStatus = _('Checking...');
			inetClassName = 'xray-health-label xray-health-checking';
			inetTitle = 'Checking internet connectivity';
		}

		if (inetInstance.status) {
			inetTitle += ' (' + inetInstance.status + ')';
		}

		return E('div', {
			'class': 'cbi-section',
			'style': 'margin-bottom:1em'
		}, [
			E('style', {}, `
				.xray-health-label {
					display: inline-block;
					padding: 0.3em 0.8em;
					border-radius: 3px;
					font-weight: bold;
					font-size: 0.9em;
					color: #454545;
					text-shadow: 0 1px 0 #fff;
					border: 1px solid;
					white-space: nowrap;
					margin-right: 0.5em;
				}
				
				[data-darkmode="true"] .xray-health-label {
					color: #f6f6f6;
					text-shadow: 0 1px 0 #4d4d4d;
				}
				
				.xray-health-connected {
					background-color: #6bdebb;
					border-color: #6bdebb;
				}
				
				[data-darkmode="true"] .xray-health-connected {
					background-color: #005F20;
					border-color: #005F20;
				}
				
				.xray-health-disconnected {
					background-color: #f8aeba;
					border-color: #f8aeba;
				}
				
				[data-darkmode="true"] .xray-health-disconnected {
					background-color: #a93734;
					border-color: #a93734;
				}
				
				.xray-health-checking,
				.xray-health-undefined {
					background-color: #dfdfdf;
					border-color: #dfdfdf;
				}
				
				[data-darkmode="true"] .xray-health-checking,
				[data-darkmode="true"] .xray-health-undefined {
					background-color: #4d4d4d;
					border-color: #4d4d4d;
				}
				
				@keyframes spin {
					0% { transform: rotate(0deg); }
					100% { transform: rotate(360deg); }
				}
				
				.xray-health-checking::after {
					content: " ⟳";
					display: inline-block;
					animation: spin 2s linear infinite;
				}
			`),
			E('div', { 'style': 'display:flex; gap:0.5em; align-items:center; flex-wrap:wrap' }, [
				E('strong', {}, _('Xray Tunnel:')),
				E('span', {
					'class': xrayClassName,
					'title': xrayTitle
				}, xrayStatus),
				E('strong', { 'style': 'margin-left:1.5em' }, _('Internet:')),
				E('span', {
					'class': inetClassName,
					'title': inetTitle
				}, inetStatus)
			])
		]);
	}
});
WIDGET_EOF

    log_info "Widget installed to $WIDGET_PATH"
}

install_rpc_backend() {
    log_info "Installing RPC backend handler..."
    
    cat > "$RPC_PATH" <<'RPC_EOF'
#!/bin/sh
# Simple RPC backend for xray-health status

STATUS_FILE="/tmp/run/xray-health/xray.status"

main() {
    case "$1" in
        list)
            echo '{"getStatus":{}}'
            ;;
        call)
            case "$2" in
                getStatus)
                    if [ -f "$STATUS_FILE" ]; then
                        cat "$STATUS_FILE"
                    else
                        echo '{"instances":[{"instance":"xray-tunnel","num":"1","inet":-1,"status":"unknown"}]}'
                    fi
                    ;;
                *)
                    echo '{"error":"Method not found"}'
                    ;;
            esac
            ;;
        *)
            echo '{"error":"Invalid command"}'
            ;;
    esac
}

main "$@"
RPC_EOF

    chmod +x "$RPC_PATH"
    log_info "RPC backend installed to $RPC_PATH"
}

install_acl() {
    log_info "Installing RPC ACL permissions..."
    
    mkdir -p "$(dirname "$ACL_PATH")"
    
    cat > "$ACL_PATH" <<'ACL_EOF'
{
	"luci-app-xray-health": {
		"description": "Grant access to xray health monitoring status",
		"read": {
			"ubus": {
				"xray-health": [ "getStatus" ]
			}
		}
	}
}
ACL_EOF

    log_info "ACL permissions installed to $ACL_PATH"
}

install_initd() {
    log_info "Installing init.d service script..."
    
    cat > "$INITD_PATH" <<'INITD_EOF'
#!/bin/sh /etc/rc.common
# Xray Health Monitor Service
# Monitors xray tunnel connectivity and manages automatic failover

START=99
STOP=01

USE_PROCD=1
PROG=/usr/local/sbin/check-xray.sh
PID_FILE=/var/run/xray-health.pid

start_service() {
	# Check if daemon script exists
	if [ ! -x "$PROG" ]; then
		echo "Error: $PROG not found or not executable"
		return 1
	fi

	procd_open_instance
	procd_set_param command "$PROG"
	procd_set_param stdout 1
	procd_set_param stderr 1
	# Don't use procd pidfile - daemon manages its own PID file
	# Respawn: wait 10 seconds after crash, retry max 3 times, reset counter after 1 hour
	procd_set_param respawn 3600 10 3
	procd_close_instance
	
	echo "Xray health monitor started"
}

stop_service() {
	if [ -f "$PID_FILE" ]; then
		local pid=$(cat "$PID_FILE")
		if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
			kill -TERM "$pid"
			echo "Stopping xray health monitor (PID $pid)..."
			
			# Wait up to 10 seconds for graceful shutdown
			local count=0
			while [ $count -lt 10 ] && kill -0 "$pid" 2>/dev/null; do
				sleep 1
				count=$((count + 1))
			done
			
			# Force kill if still running
			if kill -0 "$pid" 2>/dev/null; then
				echo "Force killing xray health monitor..."
				kill -KILL "$pid" 2>/dev/null
			fi
			
			rm -f "$PID_FILE"
		fi
	fi
	
	echo "Xray health monitor stopped"
}

service_triggers() {
	procd_add_reload_trigger "xray-health"
}
INITD_EOF

    chmod +x "$INITD_PATH"
    log_info "Init.d service installed to $INITD_PATH"
}

install_all() {
    log_info "=========================================="
    log_info "Installing Xray Health Monitor Widget"
    log_info "=========================================="
    
    # Check if daemon script exists
    if [ ! -f "$DAEMON_PATH" ]; then
        log_warn "Daemon script not found at $DAEMON_PATH"
        log_warn "Please ensure check-xray.sh is copied to $DAEMON_PATH"
    fi
    
    install_widget
    install_rpc_backend
    install_acl
    install_initd
    update_sysupgrade_conf
    
    log_info ""
    log_info "=========================================="
    log_info "Installation Complete!"
    log_info "=========================================="
    log_info ""
    log_info "Next steps:"
    log_info "1. Copy check-xray.sh to $DAEMON_PATH"
    log_info "   cp check-xray.sh $DAEMON_PATH"
    log_info "   chmod +x $DAEMON_PATH"
    log_info ""
    log_info "2. Restart rpcd and uhttpd services:"
    log_info "   /etc/init.d/rpcd restart"
    log_info "   /etc/init.d/uhttpd restart"
    log_info ""
    log_info "3. Enable and start the xray-health service:"
    log_info "   /etc/init.d/xray-health enable"
    log_info "   /etc/init.d/xray-health start"
    log_info ""
    log_info "4. Refresh your LuCI interface (Ctrl+F5)"
    log_info "   The widget will appear on Status -> Overview page"
    log_info ""
    log_info "FIRMWARE UPDATE PERSISTENCE:"
    log_info "  All files have been added to $SYSUPGRADE_CONF"
    log_info "  The widget and daemon will survive firmware upgrades!"
    log_info ""
    log_info "To check daemon status:"
    log_info "   /etc/init.d/xray-health status"
    log_info "   logread -e check-xray"
}

###############################################################################
# UNINSTALL FUNCTIONS
###############################################################################

uninstall_all() {
    log_info "=========================================="
    log_info "Uninstalling Xray Health Monitor Widget"
    log_info "=========================================="
    
    # Stop service first
    if [ -x "$INITD_PATH" ]; then
        log_info "Stopping service..."
        "$INITD_PATH" stop 2>/dev/null || true
        "$INITD_PATH" disable 2>/dev/null || true
    fi
    
    # Remove files
    [ -f "$WIDGET_PATH" ] && rm -f "$WIDGET_PATH" && log_info "Removed widget"
    [ -f "$RPC_PATH" ] && rm -f "$RPC_PATH" && log_info "Removed RPC backend"
    [ -f "$ACL_PATH" ] && rm -f "$ACL_PATH" && log_info "Removed ACL permissions"
    [ -f "$INITD_PATH" ] && rm -f "$INITD_PATH" && log_info "Removed init.d script"
    
    # Clean up runtime files
    rm -f /var/run/xray-health.pid
    rm -f /etc/xray-health.state
    rm -f /etc/xray-health-logrotate
    rm -f /root/xray-health-persistent.log*
    rm -rf /tmp/run/xray-health/
    
    log_warn "NOTE: Entries in $SYSUPGRADE_CONF were NOT removed"
    log_warn "Remove manually if needed"
    
    log_info ""
    log_info "Uninstallation complete!"
    log_info "Please restart rpcd and uhttpd:"
    log_info "   /etc/init.d/rpcd restart"
    log_info "   /etc/init.d/uhttpd restart"
}

###############################################################################
# MAIN
###############################################################################

main() {
    case "$1" in
        install)
            install_all
            ;;
        uninstall)
            uninstall_all
            ;;
        *)
            echo "Usage: $0 {install|uninstall}"
            echo ""
            echo "Commands:"
            echo "  install     - Install all xray health widget components"
            echo "  uninstall   - Remove all xray health widget components"
            exit 1
            ;;
    esac
}

main "$@"
