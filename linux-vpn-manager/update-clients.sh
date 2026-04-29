#!/bin/bash
# Update all VLESS client configs with new port, SNI, and fingerprint
# Usage: sudo bash update-clients.sh <new_port>
# Example: sudo bash update-clients.sh 48721

set -euo pipefail

CLIENT_DIR="/etc/vpn/xray/clients"
NEW_PORT="${1:-}"
NEW_SNI=""
NEW_FP="randomized"

if [ -z "$NEW_PORT" ]; then
    echo "Usage: sudo bash $0 <new_port>"
    echo "Example: sudo bash $0 48721"
    exit 1
fi

if [ ! -d "$CLIENT_DIR" ]; then
    echo "Error: Client directory $CLIENT_DIR not found"
    exit 1
fi

# Count clients
client_count=$(ls "$CLIENT_DIR"/*-config.json 2>/dev/null | wc -l)
if [ "$client_count" -eq 0 ]; then
    echo "No client configs found in $CLIENT_DIR"
    exit 1
fi

echo "=== VLESS Client Config Updater ==="
echo "Clients found: $client_count"
echo "New port: $NEW_PORT"
echo "New SNI: (empty)"
echo "New fingerprint: $NEW_FP"
echo ""

# Backup all client files
backup_dir="$CLIENT_DIR/backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
cp "$CLIENT_DIR"/*.json "$CLIENT_DIR"/*.txt "$backup_dir/" 2>/dev/null
echo "Backup saved to: $backup_dir"
echo ""

# Process each client
for config_json in "$CLIENT_DIR"/*-config.json; do
    client_base=$(basename "$config_json" -config.json)
    vless_txt="$CLIENT_DIR/${client_base}-vless.txt"
    conf_file="$CLIENT_DIR/${client_base}.conf"

    echo "Updating: $client_base"

    # Update JSON config using jq
    tmp_json=$(mktemp)
    jq --argjson port "$NEW_PORT" \
       --arg sni "$NEW_SNI" \
       --arg fp "$NEW_FP" \
       '.outbounds[0].settings.vnext[0].port = $port |
        .outbounds[0].streamSettings.realitySettings.serverName = $sni |
        .outbounds[0].streamSettings.realitySettings.fingerprint = $fp' \
       "$config_json" > "$tmp_json"
    mv "$tmp_json" "$config_json"

    # Extract values from updated JSON to regenerate VLESS URL
    uuid=$(jq -r '.outbounds[0].settings.vnext[0].users[0].id' "$config_json")
    host=$(jq -r '.outbounds[0].settings.vnext[0].address' "$config_json")
    port=$(jq -r '.outbounds[0].settings.vnext[0].port' "$config_json")
    enc=$(jq -r '.outbounds[0].settings.vnext[0].users[0].encryption' "$config_json")
    flow=$(jq -r '.outbounds[0].settings.vnext[0].users[0].flow' "$config_json")
    sni=$(jq -r '.outbounds[0].streamSettings.realitySettings.serverName' "$config_json")
    fp=$(jq -r '.outbounds[0].streamSettings.realitySettings.fingerprint' "$config_json")
    pbk=$(jq -r '.outbounds[0].streamSettings.realitySettings.publicKey' "$config_json")
    sid=$(jq -r '.outbounds[0].streamSettings.realitySettings.shortId' "$config_json")
    network=$(jq -r '.outbounds[0].streamSettings.network' "$config_json")

    # Build VLESS URL
    vless_url="vless://${uuid}@${host}:${port}?encryption=${enc}&security=reality&sni=${sni}&fp=${fp}&pbk=${pbk}&sid=${sid}&flow=${flow}&type=${network}#${host}-${client_base}"

    echo "$vless_url" > "$vless_txt"
done

echo ""
echo "=== New VLESS Links ==="
echo ""
for vless_txt in "$CLIENT_DIR"/*-vless.txt; do
    client_base=$(basename "$vless_txt" -vless.txt)
    echo "$client_base - $(cat "$vless_txt")"
    echo ""
done

echo "=== Done ==="
echo "All $client_count clients updated. Backup: $backup_dir"
echo ""
echo "IMPORTANT: Don't forget to also update the server config:"
echo "  1. Change port to $NEW_PORT in /usr/local/etc/xray/config.json"
echo "  2. Add \"\" to serverNames array"
echo "  3. Restart xray: sudo systemctl restart xray"
