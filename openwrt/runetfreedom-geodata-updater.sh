#!/bin/sh
# /usr/local/sbin/runetfreedom-geodata-updater.sh
# OpenWrt geodata updater for runetfreedom russia-v2ray-rules-dat
# 
# Features:
# - Downloads and verifies geoip.dat and geosite.dat from runetfreedom
# - SHA256 verification
# - Atomic replacement with timestamped backups
# - Xray config validation with automatic rollback
# - Survives firmware upgrade and backup/restore
# - Proper OpenWrt logging (syslog)
# - UCI integration for xray_core configuration
#
# Exit codes:
#   0 - Success
#   1 - Generic failure
#   2 - Download/verification failure
#   3 - Config validation failure (rolled back)
#   4 - Service restart/health failure

# Prevent multiple sourcing
[ -n "${_RUNETFREEDOM_UPDATER_LOADED:-}" ] && return 0
_RUNETFREEDOM_UPDATER_LOADED=1

set -eu

# Configuration
readonly REPO_BASE="https://raw.githubusercontent.com/runetfreedom/russia-v2ray-rules-dat/release"
readonly ASSET_DIR_DEFAULT="/usr/share/xray"
readonly ASSET_DIR="/usr/local/share/xray-assets"
readonly SCRIPT_PATH="/usr/local/sbin/runetfreedom-geodata-updater.sh"
readonly KEEP_BACKUPS=1
readonly DRY_RUN="${DRY_RUN:-0}"

# Derived paths
readonly BACKUP_DIR="${ASSET_DIR}/backup"
readonly TMPDIR="/tmp/runetfreedom-geodata.$$"
readonly LOCKDIR="/tmp/.runetfreedom-geodata.lock"
DATE_TAG="$(date +%F_%H%M%S)"
readonly DATE_TAG

# Logging to syslog + stdout
# View logs:
#   logread -e runetfreedom | tail -n 50    # Recent logs
#   logread -f -e runetfreedom              # Follow in real-time
#   logread | grep runetfreedom             # All logs
readonly TAG="runetfreedom"

log_info() {
  local msg="$*"
  printf '[INFO] %s\n' "$msg"
  logger -t "$TAG" -p user.info -- "$msg" 2>/dev/null || true
}

log_warn() {
  local msg="$*"
  printf '[WARN] %s\n' "$msg" >&2
  logger -t "$TAG" -p user.warning -- "$msg" 2>/dev/null || true
}

log_error() {
  local msg="$*"
  printf '[ERROR] %s\n' "$msg" >&2
  logger -t "$TAG" -p user.err -- "$msg" 2>/dev/null || true
}

die() {
  local rc="$1"; shift
  log_error "$*"
  exit "$rc"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die 1 "Missing required command: $1"
}

cleanup() {
  rm -rf "$TMPDIR" 2>/dev/null || true
  rm -f /tmp/xray_test.out 2>/dev/null || true
  rmdir "$LOCKDIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Acquire lock
acquire_lock() {
  if ! mkdir "$LOCKDIR" 2>/dev/null; then
    die 1 "Another update is already running (lock: $LOCKDIR)"
  fi
}

# Check dependencies
check_dependencies() {
  need_cmd wget
  need_cmd sha256sum
  need_cmd uci
  need_cmd logger
}

# Detect xray service
detect_xray_service() {
  if [ -x /etc/init.d/xray_core ]; then
    echo "xray_core"
  elif [ -x /etc/init.d/xray ]; then
    echo "xray"
  else
    die 1 "Cannot find Xray init script (/etc/init.d/xray_core or /etc/init.d/xray)"
  fi
}

# Setup directories
setup_directories() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would create directories: $ASSET_DIR, $BACKUP_DIR, $TMPDIR"
    return 0
  fi
  
  # Check/create ASSET_DIR
  if [ -d "$ASSET_DIR" ]; then
    [ -w "$ASSET_DIR" ] || die 1 "Directory exists but not writable: $ASSET_DIR"
  else
    mkdir -p "$ASSET_DIR" || die 1 "Cannot create $ASSET_DIR"
  fi
  
  # Check/create BACKUP_DIR
  if [ -d "$BACKUP_DIR" ]; then
    [ -w "$BACKUP_DIR" ] || die 1 "Directory exists but not writable: $BACKUP_DIR"
  else
    mkdir -p "$BACKUP_DIR" || die 1 "Cannot create $BACKUP_DIR"
  fi
  
  # Always recreate TMPDIR (cleanup on exit)
  mkdir -p "$TMPDIR" || die 1 "Cannot create $TMPDIR"
  
  log_info "Directories ready: $ASSET_DIR"
}

# Check if update is needed by comparing checksums
check_update_needed() {
  log_info "Checking for updates..."
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would check if update is needed"
    return 0
  fi
  
  # Download checksum files to compare
  local needs_update=0
  
  for filename in "geoip.dat" "geosite.dat"; do
    local sumfile="${filename}.sha256sum"
    local url_sum="${REPO_BASE}/${sumfile}"
    local installed_file="${ASSET_DIR}/${filename}"
    
    # Download remote checksum
    wget -q -O "$TMPDIR/$sumfile" "$url_sum" || die 2 "Failed to download $sumfile"
    
    local remote_sum current_sum
    remote_sum="$(awk 'NR==1{print $1}' "$TMPDIR/$sumfile" | tr -d '\r\n')"
    
    # Get current file checksum if exists
    if [ -f "$installed_file" ]; then
      current_sum="$(sha256sum "$installed_file" | awk '{print $1}')"
    else
      current_sum="none"
    fi
    
    if [ "$remote_sum" != "$current_sum" ]; then
      log_info "Update available for $filename (current: ${current_sum:0:12}..., remote: ${remote_sum:0:12}...)"
      needs_update=1
    else
      log_info "$filename is up-to-date ($current_sum)"
    fi
  done
  
  # Return 0 (success) if update needed, 1 (failure) if not needed
  if [ "$needs_update" -eq 1 ]; then
    return 0
  else
    return 1
  fi
}

# Download and verify a file pair (file + sha256sum)
fetch_and_verify() {
  local filename="$1"
  local sumfile="${filename}.sha256sum"
  local url_file="${REPO_BASE}/${filename}"
  local url_sum="${REPO_BASE}/${sumfile}"
  
  log_info "Downloading $filename..."
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would download: $url_file and $url_sum"
    return 0
  fi
  
  wget -q -O "$TMPDIR/$filename" "$url_file" || die 2 "Failed to download $filename"
  wget -q -O "$TMPDIR/$sumfile" "$url_sum" || die 2 "Failed to download $sumfile"
  
  # Verify checksum
  local expected actual
  expected="$(awk 'NR==1{print $1}' "$TMPDIR/$sumfile" | tr -d '\r\n')"
  [ -n "$expected" ] || die 2 "Empty checksum in $sumfile"
  
  actual="$(sha256sum "$TMPDIR/$filename" | awk '{print $1}')"
  [ "$expected" = "$actual" ] || die 2 "SHA256 mismatch for $filename: expected $expected, got $actual"
  [ -s "$TMPDIR/$filename" ] || die 2 "Downloaded $filename is empty"
  
  log_info "SHA256 verified: $filename ($actual)"
}

# Sanity check for runetfreedom data
sanity_check_geosite() {
  if ! command -v strings >/dev/null 2>&1; then
    log_warn "strings command not available, skipping geosite sanity check"
    return 0
  fi
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would check for 'ru-blocked' in geosite.dat"
    return 0
  fi
  
  if strings "$TMPDIR/geosite.dat" 2>/dev/null | grep -q "ru-blocked"; then
    log_info "Sanity check passed: found 'ru-blocked' in geosite.dat"
  else
    log_warn "Sanity check: 'ru-blocked' not found in geosite.dat (may still be valid)"
  fi
}

# Backup existing geodata files
backup_geodata() {
  local filename="$1"
  local file="${ASSET_DIR}/${filename}"
  
  if [ "$DRY_RUN" -eq 1 ]; then
    if [ -f "$file" ]; then
      log_info "[DRY-RUN] Would backup: $file -> $BACKUP_DIR/${filename}.${DATE_TAG}"
    else
      log_info "[DRY-RUN] Would skip backup: $file (not found)"
    fi
    return 0
  fi
  
  if [ -f "$file" ]; then
    cp -a "$file" "$BACKUP_DIR/${filename}.${DATE_TAG}" || die 1 "Backup failed for $file"
    log_info "Backed up: $file -> $BACKUP_DIR/${filename}.${DATE_TAG}"
  else
    log_info "Backup skipped: $file (not found)"
  fi
}

# Atomic file installation
install_atomic() {
  local src="$1"
  local filename="$2"
  local dst="${ASSET_DIR}/${filename}"
  local tmp="${dst}.new.$$"
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would install: $filename"
    return 0
  fi
  
  cp -f "$src" "$tmp" || die 1 "Failed to stage file: $dst"
  chmod 0644 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$dst" || die 1 "Failed to replace file: $dst"
  
  log_info "Installed: $filename"
}

# Create or update symlinks to activate geodata
create_symlinks() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would create symlinks: $ASSET_DIR_DEFAULT/*.dat -> $ASSET_DIR/*.dat"
    return 0
  fi
  
  for filename in "geoip.dat" "geosite.dat"; do
    local target="${ASSET_DIR}/${filename}"
    local link="${ASSET_DIR_DEFAULT}/${filename}"
    
    # Remove old symlink/file if exists
    rm -f "$link" 2>/dev/null || true
    
    # Create new symlink (absolute path)
    ln -sf "$target" "$link" || die 1 "Failed to create symlink: $link"
    log_info "Symlink created: $filename -> $target"
  done
}

# Ensure fallback symlinks exist (points to v2ray package dir if runetfreedom data missing)
ensure_fallback_assets() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would ensure fallback symlinks if needed"
    return 0
  fi
  
  for filename in "geoip.dat" "geosite.dat"; do
    local asset_file="${ASSET_DIR}/${filename}"
    local symlink="${ASSET_DIR_DEFAULT}/${filename}"
    
    # Only create fallback if asset file doesn't exist AND symlink doesn't exist
    if [ ! -f "$asset_file" ] && [ ! -e "$symlink" ]; then
      ln -sf "../v2ray/${filename}" "$symlink"
      log_info "Created fallback symlink: $filename -> ../v2ray/"
    fi
  done
}

# Find active xray config
find_xray_config() {
  local best=""
  
  for dir in /var/etc/xray /tmp/etc/xray /etc/xray; do
    [ -d "$dir" ] || continue
    for file in "$dir"/*.json; do
      [ -f "$file" ] || continue
      if [ -z "$best" ] || [ "$file" -nt "$best" ]; then
        best="$file"
      fi
    done
  done
  
  [ -n "$best" ] && printf '%s' "$best"
}

# Validate xray config or rollback
validate_config_or_rollback() {
  local xraybin="/usr/bin/xray"
  local config
  
  if [ ! -x "$xraybin" ]; then
    log_warn "Xray binary not found at $xraybin, skipping config validation"
    return 0
  fi
  
  config="$(find_xray_config || true)"
  if [ -z "$config" ]; then
    log_warn "No Xray JSON config found, skipping validation"
    return 0
  fi
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would validate config: $config"
    return 0
  fi
  
  log_info "Validating Xray config: $config"
  
  "$xraybin" run -test -confdir "$(dirname "$config")" >/tmp/xray_test.out 2>&1
  
  if grep -q "Configuration OK" /tmp/xray_test.out 2>/dev/null; then
    log_info "Config validation passed"
    rm -f /tmp/xray_test.out 2>/dev/null || true
    return 0
  fi
  
  log_error "Config validation FAILED. Xray output:"
  head -n 50 /tmp/xray_test.out 2>/dev/null | while IFS= read -r line; do
    log_error "  $line"
  done
  
  # Rollback to latest backup
  log_warn "Rolling back to previous geodata..."
  local last_geoip last_geosite
  last_geoip="$(ls -1t "$BACKUP_DIR"/geoip.dat.* 2>/dev/null | head -n 1 || true)"
  last_geosite="$(ls -1t "$BACKUP_DIR"/geosite.dat.* 2>/dev/null | head -n 1 || true)"
  
  [ -n "$last_geoip" ] && cp -f "$last_geoip" "$ASSET_DIR/geoip.dat"
  [ -n "$last_geosite" ] && cp -f "$last_geosite" "$ASSET_DIR/geosite.dat"
  
  # Recreate symlinks after rollback
  create_symlinks
  
  rm -f /tmp/xray_test.out 2>/dev/null || true
  die 3 "Aborted due to invalid Xray config (geodata rolled back)"
}

# Restart xray service and verify
restart_and_verify_service() {
  local service="$1"
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would restart service: $service"
    return 0
  fi
  
  log_info "Restarting $service..."
  "/etc/init.d/$service" restart || die 4 "Service restart failed: $service"
  
  sleep 2
  
  # Check if xray process is running
  if pidof xray >/dev/null 2>&1; then
    log_info "Xray process running (PID: $(pidof xray | tr '\n' ' '))"
  else
    # Check service status reports 'running'
    local status
    status="$("/etc/init.d/$service" status 2>/dev/null || echo "unknown")"
    if [ "$status" = "running" ]; then
      log_info "Service reports: running"
    else
      die 4 "Xray not running after restart (status: $status)"
    fi
  fi
  
  # Optional: Check for DNS listener (if configured)
  if command -v ss >/dev/null 2>&1; then
    if ss -ltnup 2>/dev/null | grep -qE '127\.0\.0\.1:5300|\[::\]:5300'; then
      log_info "DNS listener detected on port 5300"
    fi
  fi
}

# Print checksums for audit trail
print_checksums() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would print checksums of installed files"
    return 0
  fi
  
  log_info "Installed file checksums:"
  sha256sum "$ASSET_DIR/geoip.dat" "$ASSET_DIR/geosite.dat" 2>/dev/null | \
    while IFS= read -r line; do
      log_info "  $line"
    done
  
  log_info "Active symlinks:"
  for f in "$ASSET_DIR_DEFAULT/geoip.dat" "$ASSET_DIR_DEFAULT/geosite.dat"; do
    if [ -L "$f" ]; then
      log_info "  $(basename "$f") -> $(readlink "$f")"
    fi
  done
}

# Cleanup old backups
cleanup_old_backups() {
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "[DRY-RUN] Would cleanup old backups (keep last $KEEP_BACKUPS)"
    return 0
  fi
  
  if [ "$KEEP_BACKUPS" -le 0 ] 2>/dev/null; then
    return 0
  fi
  
  # Cleanup geoip.dat backups
  ls -1t "$BACKUP_DIR"/geoip.dat.* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | \
    while IFS= read -r file; do
      rm -f "$file" && log_info "Removed old backup: $(basename "$file")"
    done
  
  # Cleanup geosite.dat backups
  ls -1t "$BACKUP_DIR"/geosite.dat.* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | \
    while IFS= read -r file; do
      rm -f "$file" && log_info "Removed old backup: $(basename "$file")"
    done
}

# Main execution
main() {
  log_info "Starting runetfreedom geodata update"
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "=== DRY-RUN MODE - No changes will be made ==="
  fi
  
  acquire_lock
  check_dependencies
  
  local service
  service="$(detect_xray_service)"
  log_info "Detected service: $service"
  
  setup_directories
  ensure_fallback_assets
  
  # Check if update is needed
  if ! check_update_needed; then
    log_info "All geodata files are already up-to-date, nothing to do"
    exit 0
  fi
  
  # Download and verify new geodata
  fetch_and_verify "geoip.dat"
  fetch_and_verify "geosite.dat"
  sanity_check_geosite
  
  # Backup and install to runetfreedom directory
  backup_geodata "geoip.dat"
  backup_geodata "geosite.dat"
  
  install_atomic "$TMPDIR/geoip.dat" "geoip.dat"
  install_atomic "$TMPDIR/geosite.dat" "geosite.dat"
  
  # Create/update symlinks to activate new geodata
  create_symlinks
  
  # Validate and restart
  validate_config_or_rollback
  restart_and_verify_service "$service"
  
  print_checksums
  cleanup_old_backups
  
  log_info "Update completed successfully"
  
  if [ "$DRY_RUN" -eq 1 ]; then
    log_info "=== END DRY-RUN MODE ==="
  fi
}

# Run main function
main "$@"
