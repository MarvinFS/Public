#!/bin/sh
#
# DoH Speed Testing Script for OpenWRT
# Tests DNS-over-HTTPS endpoints for latency before proxying traffic
# Author: Enhanced version
# Date: 2025-12-27

# ============================================================================
# Configuration
# ============================================================================

URLS="
https://cloudflare-dns.com/dns-query
https://dns10.quad9.net/dns-query
https://dns.google/dns-query
https://doh.sb/dns-query
"

PING_COUNT=5
CONNECT_TIMEOUT=5
QUERY_TIMEOUT=10

# Colors for terminal output (if supported)
if [ -t 1 ]; then
    COLOR_RESET="\033[0m"
    COLOR_BOLD="\033[1m"
    COLOR_GREEN="\033[32m"
    COLOR_YELLOW="\033[33m"
    COLOR_RED="\033[31m"
    COLOR_CYAN="\033[36m"
else
    COLOR_RESET=""
    COLOR_BOLD=""
    COLOR_GREEN=""
    COLOR_YELLOW=""
    COLOR_RED=""
    COLOR_CYAN=""
fi

# ============================================================================
# Functions
# ============================================================================

print_header() {
    printf "\n"
    printf "${COLOR_BOLD}${COLOR_CYAN}═══════════════════════════════════════════════════════════════════════════${COLOR_RESET}\n"
    printf "${COLOR_BOLD}${COLOR_CYAN}                       DoH Speed Test Results${COLOR_RESET}\n"
    printf "${COLOR_BOLD}${COLOR_CYAN}═══════════════════════════════════════════════════════════════════════════${COLOR_RESET}\n"
    printf "\n"
    printf "${COLOR_BOLD}%-45s %-14s %-18s %-14s${COLOR_RESET}\n" \
        "DoH ENDPOINT" "PING (ms)" "CONNECT (ms)" "QUERY (ms)"
    printf "%-45s %-14s %-18s %-14s\n" \
        "---------------------------------------------" \
        "--------------" \
        "------------------" \
        "--------------"
}

colorize_value() {
    VALUE="$1"
    THRESHOLD_GOOD="$2"
    THRESHOLD_WARN="$3"
    
    if [ "$VALUE" = "NA" ] || [ "$VALUE" = "FAIL" ]; then
        printf "${COLOR_RED}${VALUE}${COLOR_RESET}"
    else
        # Convert to integer for comparison
        VALUE_INT=$(echo "$VALUE" | awk '{print int($1)}')
        if [ "$VALUE_INT" -le "$THRESHOLD_GOOD" ]; then
            printf "${COLOR_GREEN}${VALUE}${COLOR_RESET}"
        elif [ "$VALUE_INT" -le "$THRESHOLD_WARN" ]; then
            printf "${COLOR_YELLOW}${VALUE}${COLOR_RESET}"
        else
            printf "${COLOR_RED}${VALUE}${COLOR_RESET}"
        fi
    fi
}

draw_bar() {
    VALUE="$1"
    MAX="$2"
    WIDTH=20
    
    if [ "$VALUE" = "NA" ] || [ "$VALUE" = "FAIL" ]; then
        printf "[${COLOR_RED}--------------------${COLOR_RESET}]"
        return
    fi
    
    # Calculate bar length
    VALUE_INT=$(echo "$VALUE" | awk '{print int($1)}')
    BAR_LEN=$(awk -v v="$VALUE_INT" -v m="$MAX" -v w="$WIDTH" \
        'BEGIN { len=int((v/m)*w); if(len>w) len=w; print len }')
    
    # Draw bar with color
    BAR=""
    i=0
    while [ $i -lt $BAR_LEN ]; do
        BAR="${BAR}█"
        i=$((i + 1))
    done
    
    # Add spaces
    while [ $i -lt $WIDTH ]; do
        BAR="${BAR} "
        i=$((i + 1))
    done
    
    # Color the bar
    if [ "$VALUE_INT" -le 50 ]; then
        printf "[${COLOR_GREEN}${BAR}${COLOR_RESET}]"
    elif [ "$VALUE_INT" -le 150 ]; then
        printf "[${COLOR_YELLOW}${BAR}${COLOR_RESET}]"
    else
        printf "[${COLOR_RED}${BAR}${COLOR_RESET}]"
    fi
}

test_doh_query() {
    URL="$1"
    
    # Perform actual DNS query for example.com
    QUERY_TIME=$(curl -o /dev/null -s -w '%{time_total}' \
        --http2 \
        --connect-timeout "$QUERY_TIMEOUT" \
        -H "Content-Type: application/dns-json" \
        "${URL}?name=example.com&type=A" 2>/dev/null)
    
    if [ -n "$QUERY_TIME" ] && [ "$QUERY_TIME" != "0.000000" ]; then
        awk -v t="$QUERY_TIME" 'BEGIN { printf "%.2f", t*1000 }'
    else
        echo "FAIL"
    fi
}

# ============================================================================
# Main Script
# ============================================================================

print_header

# Store results in a temporary file (busybox sh doesn't support arrays)
RESULTS_FILE="/tmp/dns_speed_results_$$.tmp"
: > "$RESULTS_FILE"

BEST_PING=999999
BEST_URL=""
TEST_COUNT=0

for u in $URLS; do
    HOST=$(echo "$u" | sed 's|https://||;s|/dns-query||')
    
    # ICMP ping RTT (average)
    PING_AVG=$(ping -c "$PING_COUNT" -q "$HOST" 2>/dev/null \
        | awk -F'/' 'END { if (NF>=5) printf "%.2f", $5; else print "NA" }')
    
    # HTTPS connection time
    CONNECT=$(curl -o /dev/null -s -w '%{time_connect}' \
        --http2 --connect-timeout "$CONNECT_TIMEOUT" "$u" 2>/dev/null)
    
    if [ -n "$CONNECT" ] && [ "$CONNECT" != "0.000000" ]; then
        CONNECT_MS=$(awk -v t="$CONNECT" 'BEGIN { printf "%.2f", t*1000 }')
    else
        CONNECT_MS="NA"
    fi
    
    # Actual DoH query test
    QUERY_MS=$(test_doh_query "$u")
    
    # Store results in temp file
    printf "%s|%s|%s|%s\n" "$u" "$PING_AVG" "$CONNECT_MS" "$QUERY_MS" >> "$RESULTS_FILE"
    
    # Track best ping
    if [ "$PING_AVG" != "NA" ]; then
        PING_INT=$(echo "$PING_AVG" | awk '{print int($1)}')
        if [ "$PING_INT" -lt "$BEST_PING" ]; then
            BEST_PING="$PING_INT"
            BEST_URL="$u"
        fi
    fi
    
    # Display with colors
    PING_COLORED=$(colorize_value "$PING_AVG" 30 100)
    CONNECT_COLORED=$(colorize_value "$CONNECT_MS" 50 150)
    QUERY_COLORED=$(colorize_value "$QUERY_MS" 80 200)
    
    printf "%-45s %-23s %-27s %-23s\n" \
        "$u" "$PING_COLORED" "$CONNECT_COLORED" "$QUERY_COLORED"
    
    TEST_COUNT=$((TEST_COUNT + 1))
done

# ============================================================================
# Summary & Visualization
# ============================================================================

printf "\n"
printf "${COLOR_BOLD}${COLOR_CYAN}───────────────────────────────────────────────────────────────────────────${COLOR_RESET}\n"
printf "${COLOR_BOLD}Visualization (Query Time):${COLOR_RESET}\n"
printf "\n"

# Print visualization for each endpoint
while IFS='|' read -r url ping connect query; do
    if [ -n "$url" ]; then
        SHORT_NAME=$(echo "$url" | sed 's|https://||;s|/dns-query||')
        BAR=$(draw_bar "$query" 500)
        printf "%-30s %s %s ms\n" "$SHORT_NAME" "$BAR" "$query"
    fi
done < "$RESULTS_FILE"

# Clean up temp file
rm -f "$RESULTS_FILE"

printf "\n"
printf "${COLOR_BOLD}${COLOR_CYAN}───────────────────────────────────────────────────────────────────────────${COLOR_RESET}\n"
printf "${COLOR_BOLD}Summary:${COLOR_RESET}\n"
printf "  Tests Performed: %d\n" "$TEST_COUNT"
printf "  ${COLOR_GREEN}Good${COLOR_RESET} = Ping <30ms, Connect <50ms, Query <80ms\n"
printf "  ${COLOR_YELLOW}Fair${COLOR_RESET} = Ping <100ms, Connect <150ms, Query <200ms\n"
printf "  ${COLOR_RED}Poor/NA${COLOR_RESET} = Above thresholds or failed\n"

if [ -n "$BEST_URL" ]; then
    printf "\n"
    printf "  ${COLOR_BOLD}${COLOR_GREEN}★ Recommended:${COLOR_RESET} ${BEST_URL} (%dms ping)\n" "$BEST_PING"
fi

printf "${COLOR_BOLD}${COLOR_CYAN}═══════════════════════════════════════════════════════════════════════════${COLOR_RESET}\n"
printf "\n"

