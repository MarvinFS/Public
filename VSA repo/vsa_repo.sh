#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Veeam Software Appliance repository mirror script (v3.3)
#
# This script orchestrates the complete setup of a local Veeam VSA
# package mirror infrastructure on RHEL-family systems. It does the following:
#
#   1) Partitions and mounts: fully manages a dedicated XFS filesystem on extra data disk for repo data
#   2) Installs and checks of core packages are fine (dnf-plugins-core, curl, nginx, SELinux tools, firewalld)
#   3) Creates /etc/yum.repos.d/veeam-vsa-upstream.repo with 3 repo definitions
#   4) Generates parameterized /usr/local/sbin/veeam-vsa-reposync.sh script with injected configuration
#   5) Sets up repo directory with proper ownership and permissions
#   6) Configures nginx to serve mirror on port 80 and optionally 443 (need internet access for it and public internet routable domain)
#   7) Applies SELinux contexts and firewall rules for the repository activities and NGNINX
#   8) Creates systemd service + hourly timer for automated syncing
#   9) Validates available disk space before initial sync operation
#   10) Tests local HTTP connectivity to mirror paths to check if NGINX is correctly set
#   11) Configures logging with logrotate for reposync script
#
# NOTE: This script sets up infrastructure only. Manual reposync trigger required:
#   sudo /usr/local/sbin/veeam-vsa-reposync.sh
#
# Supported: RHEL / Rocky / Alma / CentOS Stream 9+ (incl. Rocky 10)
# NOT SUPPORTED: Debian / Ubuntu / SUSE / anything without dnf + reposync.
#
# Created by MarvinFS wrapped up with AI assistance.
# This script is NOT officially provided by Veeam Software Corporation or is supported by Veeam in any way.
# Strictly provided AS IS - use ONLY at your own discretion
# ============================================================

# -------------------------
# CONFIG START - EDIT TO REFLECT YOUR ENVIRONMENT
# -------------------------
DATA_DEVICE="/dev/sdb"              # disk to use for repo data (will be partitioned) I have added new drive to a VM 100GB
DATA_PARTITION="${DATA_DEVICE}1"    # resulting partition 
MOUNT_POINT="/mnt/data"             # mount point for XFS

REPO_ROOT="${MOUNT_POINT}/repo/repository.veeam.com"

OS_VERSION="9.2"
VBR_VERSION="13.0"

# desired hostnames and IP which NGINX serves on port HTTP and optional HTTPS you may also specify IPv6 IP - both IPv4 and v6 supported for serving.
REPO_HOSTNAME_SHORT="veeamrepo"
REPO_HOSTNAME_FQDN="veeamrepo.test.local"
REPO_HOST_IP="192.168.1.2"

# Let's Encrypt email for certificate expiry notifications (required for HTTPS)
LE_EMAIL="postmaster@test.us"
# -------------------------
# CONFIG END - EDIT TO REFLECT YOUR ENVIRONMENT
# -------------------------

# Paths and constants
UPSTREAM_BASE_URL="https://repository.veeam.com"
DNF_BIN="/usr/bin/dnf"
KEY_INDEX_URL="${UPSTREAM_BASE_URL}/keys/"
KEY_LOCAL_DIR="/etc/veeam/rpm-gpg"
UPSTREAM_REPO_FILE="/etc/yum.repos.d/veeam-vsa-upstream.repo"
REPOSYNC_SCRIPT="/usr/local/sbin/veeam-vsa-reposync.sh"
REPOSYNC_LOGFILE="/var/log/veeam-vsa-reposync.log"
REPOSYNC_LOGROTATE="/etc/logrotate.d/veeam-vsa-reposync"
NGINX_CONF="/etc/nginx/conf.d/veeam-repo.conf"

REPOID_MANDATORY="veeam-vsa-mandatory"
REPOID_OPTIONAL="veeam-vsa-optional"
REPOID_EXTERNAL_MANDATORY="veeam-vsa-external-mandatory"

SYSTEMD_SERVICE="/etc/systemd/system/veeam-vsa-reposync.service"
SYSTEMD_TIMER="/etc/systemd/system/veeam-vsa-reposync.timer"
LE_WEBROOT="/var/lib/veeam-letsencrypt"

# Runtime flags (populated via menu prompts on runtime)
ENABLE_DISK_PARTITIONING="true"
ENABLE_HTTPS="false"
LE_CERT_PATH=""
LE_KEY_PATH=""
DATA_DEVICE_PREPARED="false"

# -------------------------
# ERRORS HANDLER
# -------------------------
cleanup_on_error() {
  local exit_code=$?
  if [[ ${exit_code} -ne 0 && ${exit_code} -ne 130 ]]; then
    log "Script failed (exit code: ${exit_code}). Partial state may exist."
    log "Review the system state and re-run or use cleanup option from menu."
  fi
}
trap cleanup_on_error EXIT
trap 'log "Received interrupt signal. Exiting..."; exit 130' INT TERM

# -------------------------
# LOGGING HELPERS
# -------------------------
log() {
  printf '[%(%Y-%m-%d %H:%M:%S)T] %s\n' -1 "$*" >&2
}

fatal() {
  log "FATAL: $*"
  exit 1
}

print_colored() {
  local color="$1" text="$2"
  printf '\033[%sm%s\033[0m\n' "$color" "$text"
}

config_warning_prompt() {
  local border="==============================================================================="
  print_colored "1;31" "$border"
  print_colored "1;37" "!!! WARNING !!! Before launching the setup step, you have to edit the config section at the top of this script !!!"
  print_colored "1;33" "Defaults (hostnames, IPs, etc.) will be used otherwise, which might not be compatible with your system."
  print_colored "1;31" "$border"
  print_colored "1;32" "Press any key to continue setup or Ctrl+C to abort now."
  read -r -n 1 -s || true
}

press_any_key_to_continue() {
  printf '\nPress any key to return to the menu...\n'
  read -r -n 1 -s || true
}

display_option_menu() {
  printf '\033c\n'
  print_colored "1;36" "═══════════════════════════════════════════════════════════════════════════════"
  print_colored "1;36" "                          Veeam VSA repo mirror setup "
  print_colored "1;36" "═══════════════════════════════════════════════════════════════════════════════"
  printf '\n'
  printf '  \033[1;33m[1]\033[0m \033[1;37mFull cleanup\033[0m – remove generated services, configs, firewall rules,\n'
  printf '      and SELinux layout while keeping repo data untouched.\n\n'
  printf '  \033[1;33m[2]\033[0m \033[1;37mDisk partitioning\033[0m – if enabled, wipe/format %s and mount it at\n' "${DATA_DEVICE}"
  printf '      %s (currently: ' "${MOUNT_POINT}"
  if [[ "${ENABLE_DISK_PARTITIONING}" == "true" ]]; then
    printf '\033[1;32menabled\033[0m)\n\n'
  else
    printf '\033[1;31mdisabled\033[0m)\n\n'
  fi
  printf '  \033[1;33m[3]\033[0m \033[1;37mHTTPS \033[0m – provision with Let'"'"'s Encrypt for %s\n' "${REPO_HOSTNAME_FQDN}"
  printf '      and open TCP/443 (currently: '
  if [[ "${ENABLE_HTTPS}" == "true" ]]; then
    printf '\033[1;32menabled\033[0m)\n\n'
  else
    printf '\033[1;31mdisabled\033[0m)\n\n'
  fi
  printf '  \033[1;32m[4]\033[0m \033[1;36mSTART INSTALLATION\033[0m\n\n'
  printf '  \033[1;31m[5]\033[0m \033[1;37mExit without changes\033[0m\n'
}

perform_main_menu() {
  local choice
  while true; do
    display_option_menu
    read -r -n 1 -p $'\nSelect option [1-5]: ' choice
    printf '\n'
    case "${choice}" in
      1)
        read -r -n 1 -p $'\nType C to confirm cleanup (any other key to cancel): ' choice
        printf '\n'
        [[ "${choice,,}" == "c" ]] && cleanup_installation || log "Cleanup cancelled."
        press_any_key_to_continue
        ;;
      2)
        read -r -n 1 -p $'\nToggle disk partitioning: e=enable, d=disable, any other key to leave: ' choice
        printf '\n'
        case "${choice,,}" in
          e) ENABLE_DISK_PARTITIONING="true"; log "Disk partitioning enabled." ;;
          d) ENABLE_DISK_PARTITIONING="false"; log "Disk partitioning disabled; ${MOUNT_POINT} will be used as-is." ;;
          *) log "Disk partitioning unchanged." ;;
        esac
        press_any_key_to_continue
        ;;
      3)
        read -r -p $'\nEnter public FQDN for HTTP/HTTPS (blank to disable HTTPS): ' choice
        if [[ -n "${choice}" ]]; then
          REPO_HOSTNAME_FQDN="${choice}"
          ENABLE_HTTPS="true"
          log "HTTPS will be enabled for ${REPO_HOSTNAME_FQDN}."
        else
          ENABLE_HTTPS="false"
          log "HTTPS disabled; only HTTP will be configured."
        fi
        press_any_key_to_continue
        ;;
      4) break ;;
      5) log "Exiting per user request."; exit 0 ;;
      *) log "Invalid selection: ${choice}"; press_any_key_to_continue ;;
    esac
  done
}

# -------------------------
# BASIC CHECKS
# -------------------------
require_root() {
  [[ $EUID -eq 0 ]] || fatal "Must be run as root."
}

check_os() {
  [[ -r /etc/os-release ]] && . /etc/os-release || fatal "/etc/os-release not found. Unsupported OS."
  case "${ID:-unknown}" in
    rhel|rocky|almalinux|centos) log "OS detected: ${PRETTY_NAME:-$ID}" ;;
    *) fatal "Unsupported OS ID: ${ID:-unknown}. Only RHEL/Rocky/Alma/CentOS-family is supported." ;;
  esac
}

check_dnf() {
  command -v "${DNF_BIN}" >/dev/null 2>&1 || fatal "dnf not found. This script requires a dnf based system."
}

# -------------------------
# DISK AND FILESYSTEM SETUP
# -------------------------
prepare_disk() {
  DATA_DEVICE_PREPARED="false"
  log "Checking data disk ${DATA_DEVICE}"
  [[ -b "${DATA_DEVICE}" ]] || fatal "Device ${DATA_DEVICE} does not exist. Adjust DATA_DEVICE in script."

  # If partition already exists and is XFS, assume it is fine and skip destructive steps
  if lsblk -no TYPE "${DATA_PARTITION}" 2>/dev/null | grep -q '^part$'; then
    local fstype
    fstype=$(lsblk -no FSTYPE "${DATA_PARTITION}" 2>/dev/null || true)
    log "Found existing partition ${DATA_PARTITION} (FSTYPE=${fstype:-unknown}). Will NOT repartition or reformat."
    [[ "${fstype}" == "xfs" ]] || log "WARNING: ${DATA_PARTITION} is not XFS. Script expects XFS. Mount may fail."
    return
  fi

  log "No existing partition ${DATA_PARTITION} detected. Creating fresh GPT + XFS."
  lsblk -no MOUNTPOINT "${DATA_DEVICE}" | grep -q '/' && fatal "${DATA_DEVICE} appears to have mounted filesystems. Refusing to wipe."

  log "Wiping filesystem signatures on ${DATA_DEVICE}"
  wipefs -a "${DATA_DEVICE}"

  log "Creating GPT and single XFS partition on ${DATA_DEVICE}"
  parted "${DATA_DEVICE}" --script mklabel gpt mkpart primary xfs 0% 100%

  # Wait for kernel to update partition table (sometimes here we have race condition)
  log "Waiting for partition table to be recognized by kernel..."
  partprobe "${DATA_DEVICE}" 2>/dev/null || true
  udevadm settle --timeout=10 2>/dev/null || sleep 2

  # Verify partition exists before formatting
  local retry_count=0
  while [[ ! -b "${DATA_PARTITION}" && ${retry_count} -lt 5 ]]; do
    sleep 1
    retry_count=$((retry_count + 1))
  done
  [[ -b "${DATA_PARTITION}" ]] || fatal "Partition ${DATA_PARTITION} not found after creation. Check dmesg for errors."

  log "Formatting ${DATA_PARTITION} as XFS"
  mkfs.xfs -f "${DATA_PARTITION}"

  log "Disk preparation completed for ${DATA_PARTITION}"
  DATA_DEVICE_PREPARED="true"
}

ensure_mountpoint() {
  mkdir -p "${MOUNT_POINT}"
  chmod 0755 "${MOUNT_POINT}"
}

ensure_fstab_entry() {
  log "Ensuring /etc/fstab has PARTUUID entry for ${DATA_PARTITION} -> ${MOUNT_POINT}"
  local partuuid
  partuuid=$(blkid -o value -s PARTUUID "${DATA_PARTITION}" 2>/dev/null || true)
  [[ -n "${partuuid}" ]] || fatal "Could not read PARTUUID for ${DATA_PARTITION}. Check blkid output."

  local fstab_line="PARTUUID=${partuuid}   ${MOUNT_POINT}   xfs   defaults,noatime   0 0"
  
  # Check if PARTUUID already exists in fstab
  if grep -qw "PARTUUID=${partuuid}" /etc/fstab 2>/dev/null; then
    # PARTUUID exists - verify it points to the correct mount point
    local existing_mount
    existing_mount=$(grep -w "PARTUUID=${partuuid}" /etc/fstab | awk '{print $2}')
    
    if [[ "${existing_mount}" == "${MOUNT_POINT}" ]]; then
      log "fstab already contains correct entry for PARTUUID=${partuuid} -> ${MOUNT_POINT}. Skipping."
    else
      log "WARNING: PARTUUID=${partuuid} exists in fstab but points to '${existing_mount}' instead of '${MOUNT_POINT}'"
      log "Creating backup and updating fstab entry..."
      cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)" || log "WARNING: Could not create fstab backup"
      # Remove old entry and add new one
      sed -i "/PARTUUID=${partuuid}/d" /etc/fstab || fatal "Failed to remove old fstab entry"
      printf "\n%s\n" "${fstab_line}" >> /etc/fstab || fatal "Failed to write to /etc/fstab"
      log "Updated fstab entry: ${fstab_line}"
    fi
  else
    # PARTUUID not in fstab - add new entry
    cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)" || log "WARNING: Could not create fstab backup"
    log "Adding new fstab entry: ${fstab_line}"
    printf "\n%s\n" "${fstab_line}" >> /etc/fstab || fatal "Failed to write to /etc/fstab. Check permissions and disk space."
  fi
  log "Reloading systemd daemon and mounting all filesystems."
  systemctl daemon-reload || true
  mount -a
}

# -------------------------
# CORE PACKAGES
# -------------------------
install_core_packages() {
  log "Installing required packages (dnf-plugins-core, curl, nginx, SELinux tools, firewalld helpers, GPG)"
  "${DNF_BIN}" install -y dnf-plugins-core curl nginx policycoreutils-python-utils selinux-policy-targeted firewalld gnupg2 || \
    log "WARNING: Some packages failed to install. If your distro variant does not use them, this may be ok or not..."

  "${DNF_BIN}" --help 2>&1 | grep -q 'reposync' || fatal "dnf reposync plugin not available even after installing dnf-plugins-core."
}

# -------------------------
# DISK SPACE HELPER
# -------------------------
check_disk_space() {
  log "Checking available disk space on ${MOUNT_POINT}"
  local available_gb
  available_gb=$(df -B1G "${MOUNT_POINT}" | awk 'NR==2 {print $4}' | sed 's/G$//')

  # Validate that available_gb is a number
  if [[ -z "${available_gb}" || ! "${available_gb}" =~ ^[0-9]+$ ]]; then
    log "WARNING: Could not determine available disk space. Proceeding anyway."
    return
  fi

  if (( available_gb < 31 )); then
    log "WARNING: Only ${available_gb}GB available on ${MOUNT_POINT}. Initial reposync needs about 30-40GB."
    log "WARNING: Sync may fail if space is insufficient."
  else
    log "Disk space check: ${available_gb}GB available (sufficient for initial sync)."
  fi
}

# -------------------------
# VEEAM UPSTREAM .repo FILE
# -------------------------
create_upstream_repo_file() {
  if [[ -f "${UPSTREAM_REPO_FILE}" ]]; then
    log "Upstream repo file ${UPSTREAM_REPO_FILE} already exists. Nothing to do."
    return
  fi

  log "Creating upstream repo definition at ${UPSTREAM_REPO_FILE}"
  
  # Dynamically fetch and validate GPG keys from upstream before adding to config
  log "Fetching available GPG keys from ${KEY_INDEX_URL}"
  local key_index
  key_index=$(curl --connect-timeout 30 --max-time 60 -fsSL "${KEY_INDEX_URL}" 2>/dev/null) || \
    fatal "Failed to fetch GPG key index from ${KEY_INDEX_URL}. Check network connectivity."
  
  local key_list
  # Extract key filenames from HTML index (nginx autoindex format: href="filename")
  # Match RPM-*, veeam.gpg, and hex key IDs, excluding DEB-* (Debian keys not needed for RPM systems)
  key_list=$(printf '%s\n' "${key_index}" | grep -oE 'href="[^"]*"' | sed 's/href="//;s/"$//' | grep -E '^(RPM-|veeam\.gpg|[A-F0-9]{8})' | grep -v '^DEB-' | sort -u || true)
  [[ -n "${key_list}" ]] || \
    fatal "No signing keys found in ${KEY_INDEX_URL}. Upstream structure may have changed."
  
  # Import keys locally first to validate them before adding to repo config
  mkdir -p "${KEY_LOCAL_DIR}"
  local gpgkeys=""
  local validated_count=0
  
  for key_name in ${key_list}; do
    local key_url="${KEY_INDEX_URL}${key_name}"
    local key_file="${KEY_LOCAL_DIR}/${key_name}.gpg"
    
    log "Downloading and validating key: ${key_name}"
    if curl --connect-timeout 30 --max-time 60 -fsSL "${key_url}" -o "${key_file}" 2>/dev/null; then
      if rpm --import "${key_file}" 2>/dev/null; then
        gpgkeys="${gpgkeys}${key_url} "
        validated_count=$((validated_count + 1))
        log "  ✓ ${key_name} imported successfully"
      else
        log "  ✗ ${key_name} failed rpm import, skipping"
        rm -f "${key_file}"
      fi
    else
      log "  ✗ ${key_name} download failed, skipping"
    fi
  done
  
  [[ ${validated_count} -gt 0 ]] || \
    fatal "Failed to import any GPG keys from ${KEY_INDEX_URL}. Cannot proceed without valid signing keys."
  
  gpgkeys="${gpgkeys% }"  # Remove trailing space
  log "Successfully validated and imported ${validated_count} GPG key(s)"

  tee "${UPSTREAM_REPO_FILE}" >/dev/null <<EOF
[${REPOID_MANDATORY}]
name=Veeam VSA mandatory (upstream mirror source)
baseurl=${UPSTREAM_BASE_URL}/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory/
enabled=0
gpgcheck=0
repo_gpgcheck=0
gpgkey=${gpgkeys}

[${REPOID_OPTIONAL}]
name=Veeam VSA optional (upstream mirror source)
baseurl=${UPSTREAM_BASE_URL}/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/optional/
enabled=0
gpgcheck=0
repo_gpgcheck=0
gpgkey=${gpgkeys}

[${REPOID_EXTERNAL_MANDATORY}]
name=Veeam VSA external mandatory (upstream mirror source)
baseurl=${UPSTREAM_BASE_URL}/vsa/${OS_VERSION}/external-mandatory/
enabled=0
gpgcheck=0
repo_gpgcheck=0
gpgkey=${gpgkeys}
EOF
}

# -------------------------
# LOGROTATE CONFIG
# -------------------------
create_logrotate_config() {
  log "Creating logrotate configuration at ${REPOSYNC_LOGROTATE}"
  tee "${REPOSYNC_LOGROTATE}" >/dev/null <<'EOF'
/var/log/veeam-vsa-reposync.log {
    monthly
    rotate 3
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root root
}
EOF
  chmod 0644 "${REPOSYNC_LOGROTATE}"
}

# -------------------------
# REPOSYNC SCRIPT
# -------------------------
create_reposync_script() {
  log "Creating reposync script at ${REPOSYNC_SCRIPT}"
  mkdir -p "$(dirname "${REPOSYNC_SCRIPT}")"

  cat <<'REPOSYNC_TEMPLATE' > "${REPOSYNC_SCRIPT}"
#!/usr/bin/env bash
set -eo pipefail

# ============================================================
# Veeam VSA local mirror script using dnf reposync
# Supported: RHEL / Rocky / Alma / CentOS Stream 9+ (incl. Rocky 10)
# NOT SUPPORTED: Debian/Ubuntu or non-dnf systems.
# Created by MarvinFS wrapped up with AI assistance.
# This script is NOT officially provided by Veeam Software Corporation or is supported by Veeam in any way.
# Strictly provided AS IS - use ONLY at your own discretion
#
# NOTE:
#  - GPG checks are DISABLED on the mirror host (--nogpgcheck),
#  - The VSA itself can still enforce full GPG checks (packages
#    and metadata) when using this mirror.
#  - dnf reposync handles package integrity verification automatically
#    (corrupted files are re-downloaded on next sync)
# ============================================================

REPOSYNC_TEMPLATE

  # Now append the variables with proper expansion
  cat <<REPOSYNC_VARS >> "${REPOSYNC_SCRIPT}"
REPO_ROOT="${REPO_ROOT}"
OS_VERSION="${OS_VERSION}"
VBR_VERSION="${VBR_VERSION}"

REPOID_MANDATORY="${REPOID_MANDATORY}"
REPOID_OPTIONAL="${REPOID_OPTIONAL}"
REPOID_EXTERNAL_MANDATORY="${REPOID_EXTERNAL_MANDATORY}"

# Upstream Veeam URLs for VSA repos (metadata signatures live here)
UPSTREAM_BASE_URL="${UPSTREAM_BASE_URL}"
UPSTREAM_MANDATORY_URL="\${UPSTREAM_BASE_URL}/vsa/\${OS_VERSION}/vbr/\${VBR_VERSION}/mandatory"
UPSTREAM_OPTIONAL_URL="\${UPSTREAM_BASE_URL}/vsa/\${OS_VERSION}/vbr/\${VBR_VERSION}/optional"
UPSTREAM_EXTERNAL_MANDATORY_URL="\${UPSTREAM_BASE_URL}/vsa/\${OS_VERSION}/external-mandatory"

DNF_BIN="${DNF_BIN}"
KEY_INDEX_URL="\${UPSTREAM_BASE_URL}/keys/"
KEY_LOCAL_DIR="${KEY_LOCAL_DIR}"

# Logging
LOGFILE="${REPOSYNC_LOGFILE}"
REPOSYNC_VARS

  # Append the rest of the script without variable expansion
  cat <<'REPOSYNC_FUNCTIONS' >> "${REPOSYNC_SCRIPT}"

# Session tracking
SESSION_START=""
SESSION_STATUS="SUCCESS"

log() {
  local msg
  msg=$(printf '[%(%Y-%m-%d %H:%M:%S)T] %s' -1 "$*")
  printf '%s\n' "$msg" >&2
  printf '%s\n' "$msg" >> "$LOGFILE"
}

cleanup_temp_files() {
  rm -f /tmp/veeam-reposync-*.txt 2>/dev/null || true
}

# Ensure temp files are cleaned up on exit
trap cleanup_temp_files EXIT

check_prereqs() {
  [[ $EUID -eq 0 ]] || { log "ERROR: Must run as root."; exit 1; }

  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
  else
    log "ERROR: /etc/os-release not found. Unsupported OS."
    exit 1
  fi

  case "${ID:-unknown}" in
    rhel|rocky|almalinux|centos) ;;
    *) log "ERROR: This script supports only RHEL/Rocky/Alma/CentOS-family systems."; exit 1 ;;
  esac

  command -v "${DNF_BIN}" >/dev/null 2>&1 || { log "ERROR: dnf not found. This is not a RHEL-family system."; exit 1; }
  "${DNF_BIN}" --help 2>&1 | grep -q 'reposync' || { log "ERROR: dnf reposync plugin not available. Install dnf-plugins-core first: dnf install -y dnf-plugins-core"; exit 1; }
  command -v curl >/dev/null 2>&1 || { log "ERROR: curl not found. Install it first: dnf install -y curl"; exit 1; }
  [[ -f /etc/yum.repos.d/veeam-vsa-upstream.repo ]] || { log "ERROR: /etc/yum.repos.d/veeam-vsa-upstream.repo not found. Create it before running this script."; exit 1; }
}

sync_veeam_keys() {
  log "Syncing Veeam signing keys from ${KEY_INDEX_URL}"
  mkdir -p "${KEY_LOCAL_DIR}"

  local index
  index=$(curl --connect-timeout 30 --max-time 60 -fsSL "${KEY_INDEX_URL}" 2>/dev/null) || { log "WARNING: Failed to download key index, skipping key sync."; return; }

  local keys
  # Extract key filenames from HTML index (nginx autoindex format: href="filename")
  keys=$(printf '%s\n' "${index}" | grep -oE 'href="[^"]*"' | sed 's/href="//;s/"$//' | grep -E '^(RPM-|veeam\.gpg|[A-F0-9]{8})' | grep -v '^DEB-' | sort -u || true)
  [[ -n "${keys}" ]] || { log "WARNING: No signing keys found in index, skipping key sync."; return; }

  local k success_count=0
  for k in ${keys}; do
    local dst="${KEY_LOCAL_DIR}/${k}.gpg"
    local url="${KEY_INDEX_URL}${k}"

    if curl --connect-timeout 30 --max-time 60 -fsSL "${url}" -o "${dst}" 2>/dev/null; then
      if command -v gpg >/dev/null 2>&1 && gpg --batch --quiet --yes --import "${dst}" >/dev/null 2>&1; then
        command -v rpm >/dev/null 2>&1 && rpm --import "${dst}" >/dev/null 2>&1 || true
        log "  ✓ Imported key: ${k}"
        success_count=$((success_count + 1))
      fi
    fi
  done

  log "GPG key sync completed: ${success_count} key(s) imported successfully"
}

sync_repo_signatures() {
  local upstream_url="$1"
  local target_path="$2"
  local repodata_path="${target_path}/repodata"

  mkdir -p "${repodata_path}"

  local f
  for f in repomd.xml.asc repomd.xml.key; do
    local url="${upstream_url}/repodata/${f}"
    local dst="${repodata_path}/${f}"

    if curl --connect-timeout 30 --max-time 60 -fsSL "${url}" -o "${dst}" 2>/dev/null; then
      : # Success - silent
    else
      log "WARNING: Could not download ${f} from ${url}"
    fi
  done
}

log_package_changes() {
  local repoid="$1"
  local before_file="$2"
  local after_file="$3"

  # Compare sorted lists
  local added removed
  added=$(comm -13 "$before_file" "$after_file" 2>/dev/null || true)
  removed=$(comm -23 "$before_file" "$after_file" 2>/dev/null || true)

  local added_count=0 removed_count=0
  [[ -n "$added" ]] && added_count=$(printf '%s\n' "$added" | wc -l)
  [[ -n "$removed" ]] && removed_count=$(printf '%s\n' "$removed" | wc -l)

  if [[ $added_count -eq 0 && $removed_count -eq 0 ]]; then
    log "  ${repoid}: No package changes"
    return
  fi

  log "  ${repoid}: +${added_count} added, -${removed_count} removed"

  if [[ -n "$added" ]]; then
    log "  [Added]"
    while IFS= read -r rpm_path; do
      [[ -n "$rpm_path" ]] && log "    ${rpm_path##*/}"
    done <<< "$added"
  fi

  if [[ -n "$removed" ]]; then
    log "  [Removed]"
    while IFS= read -r rpm_path; do
      [[ -n "$rpm_path" ]] && log "    ${rpm_path##*/}"
    done <<< "$removed"
  fi
}

mirror_repo() {
  local repoid="$1"
  local relpath="$2"
  local upstream_url="$3"
  local target_path="${REPO_ROOT}/${relpath}"

  mkdir -p "${target_path}"

  log "Running dnf reposync for ${repoid}"
  log "Target: ${target_path}"

  # Capture package list before sync
  local before_file="/tmp/veeam-reposync-before-${repoid}.txt"
  local after_file="/tmp/veeam-reposync-after-${repoid}.txt"
  find "${target_path}" -name "*.rpm" 2>/dev/null | sort > "$before_file" || true

  # IMPORTANT:
  #   --nogpgcheck is ONLY for this mirror host.
  #   The VSA still does GPG checks when consuming this mirror.
  #   dnf reposync automatically verifies and re-downloads corrupted packages.
  "${DNF_BIN}" -y reposync \
    --repoid="${repoid}" \
    --download-metadata \
    --download-path="${target_path}" \
    --norepopath \
    --delete \
    --nogpgcheck

  # Capture package list after sync
  find "${target_path}" -name "*.rpm" 2>/dev/null | sort > "$after_file" || true

  # Log package changes
  log_package_changes "${repoid}" "$before_file" "$after_file"

  # After successful reposync, pull metadata signature files
  sync_repo_signatures "${upstream_url}" "${target_path}"
}

verify_repo_metadata() {
  local relpath="$1"
  local target_path="${REPO_ROOT}/${relpath}"
  local repodata_path="${target_path}/repodata"
  local asc="${repodata_path}/repomd.xml.asc"
  local xml="${repodata_path}/repomd.xml"

  [[ -f "${asc}" && -f "${xml}" ]] || { log "!FATAL: Missing repomd.xml or repomd.xml.asc in ${repodata_path}"; return 1; }
  command -v gpg >/dev/null 2>&1 || { log "!FATAL: gpg not found; cannot verify metadata"; return 1; }

  log "Verifying metadata signature: ${asc}"
  
  # Run GPG verification and capture output (--batch --no-tty prevents hanging in automated scripts)
  local gpg_output gpg_status
  gpg_output=$(gpg --batch --no-tty --verify "${asc}" "${xml}" 2>&1)
  gpg_status=$?
  
  # Display GPG output - filter out confusing warnings and trust messages, show only clean verification
  local filtered_output
  filtered_output=$(printf '%s\n' "${gpg_output}" | grep -E '(Signature made|using RSA key|Good signature|BAD signature)' | sed 's/ \[unknown\]$//' || true)
  [[ -n "$filtered_output" ]] && log "$filtered_output"
  
  # Check verification result and log appropriately
  if [[ ${gpg_status} -eq 0 ]]; then
    log "!SUCCESS: Metadata signature verified for ${relpath}"
    return 0
  else
    log "!FATAL: Metadata signature FAILED for ${relpath}"
    return 1
  fi
}

verify_all_metadata_or_abort() {
  local failed="false"

  log "============================================================"
  log "VERIFYING REPOSITORY METADATA SIGNATURES"
  log "============================================================"

  verify_repo_metadata "vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory" || failed="true"
  verify_repo_metadata "vsa/${OS_VERSION}/vbr/${VBR_VERSION}/optional" || failed="true"
  verify_repo_metadata "vsa/${OS_VERSION}/external-mandatory" || failed="true"

  if [[ "${failed}" == "true" ]]; then
    log "============================================================"
    log "!FATAL: REPOSITORY METADATA SIGNATURE VERIFICATION FAILED"
    log "This may indicate tampering or corruption of upstream metadata."
    log "Taking mirror offline. Manual investigation required."
    log "============================================================"
    SESSION_STATUS="FAILURE"
    command -v systemctl >/dev/null 2>&1 && systemctl stop nginx || log "WARNING: Failed to stop nginx; please stop it manually."
    exit 1
  fi

  log "============================================================"
  log "ALL REPOSITORY METADATA SIGNATURES VERIFIED SUCCESSFULLY"
  log "Repository integrity is intact - mirror is ready to serve"
  log "============================================================"
}

log_session_start() {
  SESSION_START=$(date +%s)
  log "============================================================"
  log "REPOSYNC SESSION START"
  log "============================================================"
}

log_session_end() {
  local end_time duration_sec duration_str
  end_time=$(date +%s)
  duration_sec=$((end_time - SESSION_START))
  
  # Format duration as Xm Ys
  local mins=$((duration_sec / 60))
  local secs=$((duration_sec % 60))
  if [[ $mins -gt 0 ]]; then
    duration_str="${mins}m ${secs}s"
  else
    duration_str="${secs}s"
  fi

  log "============================================================"
  log "REPOSYNC SESSION END - STATUS: ${SESSION_STATUS}"
  log "Duration: ${duration_str}"
  log "============================================================"
}

main() {
  log_session_start

  check_prereqs
  sync_veeam_keys

  # Mirror the three VSA repos into the expected tree
  mirror_repo "${REPOID_MANDATORY}"          "vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory"       "${UPSTREAM_MANDATORY_URL}"
  mirror_repo "${REPOID_OPTIONAL}"           "vsa/${OS_VERSION}/vbr/${VBR_VERSION}/optional"        "${UPSTREAM_OPTIONAL_URL}"
  mirror_repo "${REPOID_EXTERNAL_MANDATORY}" "vsa/${OS_VERSION}/external-mandatory"                 "${UPSTREAM_EXTERNAL_MANDATORY_URL}"

  verify_all_metadata_or_abort

  log "Veeam VSA reposync completed successfully."
  log_session_end
}

main "$@"
REPOSYNC_FUNCTIONS

  chmod 0755 "${REPOSYNC_SCRIPT}"
}

# -------------------------
# NGINX CONFIG
# -------------------------
build_nginx_http_config() {
  local config=""
  config+="server {\n"
  config+="    listen 80;\n"
  config+="    listen [::]:80;\n"
  config+="    server_name ${REPO_HOSTNAME_FQDN} ${REPO_HOSTNAME_SHORT} ${REPO_HOST_IP};\n"
  
  if [[ "${ENABLE_HTTPS}" == "true" ]]; then
    config+="\n"
    config+="    location /.well-known/acme-challenge/ {\n"
    config+="        root ${LE_WEBROOT};\n"
    config+="    }\n"
  fi
  
  config+="\n"
  config+="    root ${REPO_ROOT};\n"
  config+="    autoindex on;\n"
  config+="    autoindex_exact_size off;\n"
  config+="    autoindex_localtime on;\n"
  config+="\n"
  config+="    location / {\n"
  config+="        try_files \$uri \$uri/ =404;\n"
  config+="    }\n"
  config+="}\n"
  
  printf '%b' "${config}"
}

build_nginx_https_config() {
  local config=""
  
  # HTTP server block with redirect
  config+="server {\n"
  config+="    listen 80;\n"
  config+="    listen [::]:80;\n"
  config+="    server_name ${REPO_HOSTNAME_FQDN} ${REPO_HOSTNAME_SHORT} ${REPO_HOST_IP};\n"
  config+="\n"
  config+="    location /.well-known/acme-challenge/ {\n"
  config+="        root ${LE_WEBROOT};\n"
  config+="    }\n"
  config+="\n"
  config+="    return 301 https://\$host\$request_uri;\n"
  config+="}\n"
  config+="\n"
  
  # HTTPS server block (hardened for public internet exposure)
  config+="server {\n"
  config+="    listen 443 ssl;\n"
  config+="    listen [::]:443 ssl;\n"
  config+="    http2 on;\n"
  config+="    server_name ${REPO_HOSTNAME_FQDN};\n"
  config+="\n"
  config+="    ssl_certificate ${LE_CERT_PATH};\n"
  config+="    ssl_certificate_key ${LE_KEY_PATH};\n"
  config+="    include /etc/letsencrypt/options-ssl-nginx.conf;\n"
  config+="    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n"
  config+="\n"
  config+="    # Security hardening\n"
  config+="    server_tokens off;\n"
  config+="    \n"
  config+="    # Only allow GET and HEAD methods (sufficient for repo browsing)\n"
  config+="    if (\$request_method !~ ^(GET|HEAD)\$ ) {\n"
  config+="        return 405;\n"
  config+="    }\n"
  config+="\n"
  config+="    # Security headers\n"
  config+="    add_header X-Content-Type-Options \"nosniff\" always;\n"
  config+="    add_header X-Frame-Options \"DENY\" always;\n"
  config+="    add_header X-XSS-Protection \"1; mode=block\" always;\n"
  config+="    add_header Referrer-Policy \"strict-origin-when-cross-origin\" always;\n"
  config+="\n"
  config+="    # Rate limiting zone (defined in http block via /etc/nginx/nginx.conf)\n"
  config+="    # Uncomment if limit_req_zone is configured: limit_req zone=repo_limit burst=50 nodelay;\n"
  config+="\n"
  config+="    root ${REPO_ROOT};\n"
  config+="    autoindex on;\n"
  config+="    autoindex_exact_size off;\n"
  config+="    autoindex_localtime on;\n"
  config+="\n"
  config+="    # Restrict to repo file types only\n"
  config+="    location / {\n"
  config+="        # Block access to hidden files (dotfiles)\n"
  config+="        location ~ /\\. {\n"
  config+="            deny all;\n"
  config+="            return 404;\n"
  config+="        }\n"
  config+="        try_files \$uri \$uri/ =404;\n"
  config+="    }\n"
  config+="}\n"
  
  printf '%b' "${config}"
}

configure_nginx() {
  local mode="$1"
  log "Ensuring nginx is enabled and running"
  systemctl enable --now nginx || fatal "Failed to enable or start nginx."

  # Backup existing config if present
  if [[ -f "${NGINX_CONF}" ]]; then
    cp "${NGINX_CONF}" "${NGINX_CONF}.bak.$(date +%Y%m%d%H%M%S)" || log "WARNING: Could not backup nginx config"
  fi

  log "Writing nginx configuration (${mode}) to ${NGINX_CONF}"

  if [[ "${mode}" == "https" ]]; then
    [[ -n "${LE_CERT_PATH}" && -n "${LE_KEY_PATH}" ]] || fatal "HTTPS mode requested but certificate paths are not set."
    build_nginx_https_config > "${NGINX_CONF}"
  else
    build_nginx_http_config > "${NGINX_CONF}"
  fi

  log "Testing nginx configuration"
  nginx -t
  log "Reloading nginx"
  systemctl reload nginx
}

# -------------------------
# SELINUX CONTEXTS
# -------------------------
configure_selinux() {
  if ! command -v getenforce >/dev/null 2>&1; then
    log "getenforce not available. Assuming SELinux not in use, skipping."
    return
  fi

  local mode
  mode=$(getenforce || echo "Unknown")
  log "SELinux mode: ${mode}"

  case "${mode}" in
    Enforcing|Permissive)
      log "Applying SELinux context httpd_sys_content_t to ${MOUNT_POINT}/repo"
      semanage fcontext -a -t httpd_sys_content_t "${MOUNT_POINT}/repo(/.*)?" || \
        log "WARNING: semanage failed. Check if policycoreutils-python-utils is installed."
      restorecon -Rv "${MOUNT_POINT}/repo" || \
        log "WARNING: restorecon failed. Check SELinux configuration."
      ;;
    *)
      log "SELinux not enforcing or permissive. Skipping context configuration."
      ;;
  esac
}

# -------------------------
# FIREWALLD
# -------------------------
configure_firewall() {
  local enable_https="$1"
  if ! systemctl is-active --quiet firewalld; then
    log "firewalld is not active. Skipping firewall configuration."
    return
  fi

  log "Opening HTTP service in firewalld"
  firewall-cmd --add-service=http --permanent || log "WARNING: failed to add http service to firewalld."

  if [[ "${enable_https}" == "true" ]]; then
    log "Opening HTTPS service in firewalld"
    firewall-cmd --add-service=https --permanent || log "WARNING: failed to add https service to firewalld."
  fi

  firewall-cmd --reload || log "WARNING: failed to reload firewalld."
}

setup_lets_encrypt() {
  [[ -n "${REPO_HOSTNAME_FQDN}" ]] || fatal "REPO_HOSTNAME_FQDN is empty while attempting to configure HTTPS."
  [[ "${LE_EMAIL}" != "user@test.email" ]] || fatal "LE_EMAIL is still set to default 'user@test.email'. Please update the CONFIG section with a valid email address for Let's Encrypt certificate expiry notifications."

  log "Installing certbot tools"
  "${DNF_BIN}" install -y certbot python3-certbot-nginx || fatal "Failed to install certbot packages."

  mkdir -p "${LE_WEBROOT}"
  chmod 0755 "${LE_WEBROOT}"

  if [[ -d "/etc/letsencrypt/live/${REPO_HOSTNAME_FQDN}" ]]; then
    log "Existing Let's Encrypt certificate found for ${REPO_HOSTNAME_FQDN}; reusing it."
  else
    log "Requesting Let's Encrypt certificate for ${REPO_HOSTNAME_FQDN}"
    log "NOTE: Ensure port 80 is accessible from the internet for ACME challenge."
    certbot certonly --webroot -w "${LE_WEBROOT}" --non-interactive --agree-tos --email "${LE_EMAIL}" -d "${REPO_HOSTNAME_FQDN}" || \
      fatal "certbot failed to obtain certificate for ${REPO_HOSTNAME_FQDN}. Manually test port 80 connection to this host from puiblic internet."
  fi

  LE_CERT_PATH="/etc/letsencrypt/live/${REPO_HOSTNAME_FQDN}/fullchain.pem"
  LE_KEY_PATH="/etc/letsencrypt/live/${REPO_HOSTNAME_FQDN}/privkey.pem"
  [[ -f "${LE_CERT_PATH}" && -f "${LE_KEY_PATH}" ]] || fatal "Certificate files not found after certbot run."

  log "Let's Encrypt certificate ready for ${REPO_HOSTNAME_FQDN}"
  ensure_le_tls_defaults
}

ensure_le_tls_defaults() {
  local options_conf="/etc/letsencrypt/options-ssl-nginx.conf"
  local dhparam_file="/etc/letsencrypt/ssl-dhparams.pem"
  local dhparam_url="https://raw.githubusercontent.com/certbot/certbot/master/certbot/certbot/ssl-dhparams.pem"

  if [[ ! -f "${options_conf}" ]]; then
    log "Creating default nginx TLS options at ${options_conf}"
    cat <<'EOF' > "${options_conf}"
ssl_protocols TLSv1.2 TLSv1.3;
ssl_prefer_server_ciphers off;
ssl_ciphers ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
ssl_session_timeout 1d;
ssl_session_cache shared:LE_CACHE:10m;
ssl_session_tickets off;
ssl_stapling on;
ssl_stapling_verify on;
add_header Strict-Transport-Security "max-age=63072000" always;
EOF
    chmod 0644 "${options_conf}"
  fi

  if [[ ! -f "${dhparam_file}" ]]; then
    log "Ensuring nginx DH params at ${dhparam_file}"
    if command -v curl >/dev/null 2>&1 && curl --connect-timeout 30 --max-time 60 -fsSL "${dhparam_url}" -o "${dhparam_file}"; then
      log "Downloaded default DH params from Certbot repository."
    else
      log "Downloading DH params failed; generating locally (this may take a while)."
      openssl dhparam -out "${dhparam_file}" 2048 || fatal "Failed to obtain DH params for nginx."
    fi
    chmod 0644 "${dhparam_file}"
  fi
}

# -------------------------
# CONNECTIVITY CHECKS
# -------------------------
check_connectivity() {
  local enable_https="$1"
  if ! command -v curl >/dev/null 2>&1; then
    log "curl not available for connectivity checks. Skipping."
    return
  fi

  log "Checking if local NGINX is serving HTTP/S repo paths correctly:"
  local mandatory_path="vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory/"
  local urls=(
    "http://${REPO_HOSTNAME_SHORT}/${mandatory_path}"
    "http://${REPO_HOSTNAME_FQDN}/${mandatory_path}"
    "http://${REPO_HOST_IP}/${mandatory_path}"
  )

  [[ "${enable_https}" == "true" ]] && urls+=("https://${REPO_HOSTNAME_FQDN}/${mandatory_path}")

  local u
  for u in "${urls[@]}"; do
    log "  - curl -I ${u}"
    curl --connect-timeout 5 -I -s "${u}" >/dev/null && log "    OK: ${u}" || log "    WARNING: failed to connect to ${u}"
  done
}

# -------------------------
# SYSTEMD SERVICE + TIMER
# -------------------------
create_systemd_units() {
  log "Creating systemd service at ${SYSTEMD_SERVICE}"
  tee "${SYSTEMD_SERVICE}" >/dev/null <<EOF
[Unit]
Description=Veeam VSA repository mirror sync
Documentation=file:${REPOSYNC_SCRIPT}
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=${REPOSYNC_SCRIPT}
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EOF

  log "Creating systemd timer at ${SYSTEMD_TIMER}"
  tee "${SYSTEMD_TIMER}" >/dev/null <<EOF
[Unit]
Description=Run Veeam VSA repository mirror sync hourly

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

  log "Reloading systemd units and enabling timer"
  systemctl daemon-reload
  systemctl enable --now veeam-vsa-reposync.timer
}

cleanup_installation() {
  log "============================================================"
  log "Cleanup requested. Repository data under ${REPO_ROOT} will be preserved."

  # Check if unit actually exists before trying to disable
  if systemctl cat veeam-vsa-reposync.timer &>/dev/null; then
    systemctl disable --now veeam-vsa-reposync.timer 2>/dev/null || log "WARNING: Failed to disable timer"
  fi

  if systemctl cat veeam-vsa-reposync.service &>/dev/null; then
    systemctl disable --now veeam-vsa-reposync.service 2>/dev/null || log "WARNING: Failed to disable service"
  fi

  rm -f "${SYSTEMD_TIMER}" "${SYSTEMD_SERVICE}" "${NGINX_CONF}" "${UPSTREAM_REPO_FILE}" "${REPOSYNC_SCRIPT}" "${REPOSYNC_LOGROTATE}"
  rm -rf "${KEY_LOCAL_DIR}" "${LE_WEBROOT}"

  if command -v nginx >/dev/null 2>&1; then
    nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || \
      log "WARNING: nginx config test failed during cleanup; manual review may be required."
  fi

  if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --remove-service=http >/dev/null 2>&1 || true
    firewall-cmd --permanent --remove-service=https >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
  fi

  command -v semanage >/dev/null 2>&1 && \
    semanage fcontext -d -t httpd_sys_content_t "${MOUNT_POINT}/repo(/.*)?" >/dev/null 2>&1 || true

  [[ -d "/etc/systemd/system" ]] && systemctl daemon-reload >/dev/null 2>&1 || true

  log "Cleanup complete. User data at ${REPO_ROOT} was not removed."
  log "Log file preserved at: ${REPOSYNC_LOGFILE}"
  log "============================================================"
}

# -------------------------
# MAIN
# -------------------------
main() {
  require_root
  config_warning_prompt
  perform_main_menu

  check_os
  check_dnf

  # 1) Disk and filesystem setup
  local manage_disk="false"
  if [[ "${ENABLE_DISK_PARTITIONING}" == "true" ]]; then
    prepare_disk
    [[ "${DATA_DEVICE_PREPARED}" == "true" ]] && manage_disk="true" || \
      log "Disk partitioning was skipped because ${DATA_PARTITION} already exists; no fstab entry will be added."
  fi

  if [[ "${manage_disk}" == "true" ]]; then
    ensure_mountpoint
    ensure_fstab_entry
  else
    mkdir -p "${MOUNT_POINT}"
    chmod 0755 "${MOUNT_POINT}"
  fi
  check_disk_space

  # 2) Package installation
  install_core_packages

  # 3) Upstream repo configuration
  create_upstream_repo_file

  # 4) Logrotate configuration
  create_logrotate_config

  # 5) Reposync script generation (with injected configuration)
  create_reposync_script

  # 6) Directory preparation
  log "Ensuring repo root directory ${REPO_ROOT}"
  mkdir -p "${REPO_ROOT}"
  chown root:root "${MOUNT_POINT}" "${MOUNT_POINT}/repo" "${REPO_ROOT}" || true
  chmod 0755 "${MOUNT_POINT}" "${MOUNT_POINT}/repo" "${REPO_ROOT}" || true

  # 7) Network and security configuration
  [[ "${ENABLE_HTTPS}" == "true" ]] && mkdir -p "${LE_WEBROOT}" && chmod 0755 "${LE_WEBROOT}"

  configure_nginx "http"
  configure_selinux
  configure_firewall "${ENABLE_HTTPS}"

  if [[ "${ENABLE_HTTPS}" == "true" ]]; then
    setup_lets_encrypt
    configure_nginx "https"
  fi

  # 8) Systemd automation setup
  create_systemd_units

  # 9) Connectivity test
  check_connectivity "${ENABLE_HTTPS}"

  log "============================================================"
  log "Veeam VSA mirror setup completed."
  log "Serve URL examples:"
  log "  http://${REPO_HOSTNAME_SHORT}/vsa"
  log "  http://${REPO_HOSTNAME_FQDN}/vsa"
  log "  http://${REPO_HOST_IP}/vsa"
  [[ "${ENABLE_HTTPS}" == "true" ]] && log "HTTPS is enabled for ${REPO_HOSTNAME_FQDN}" && log "  https://${REPO_HOSTNAME_FQDN}/vsa"
  log ""
  log "Hourly sync is handled by systemd timer: veeam-vsa-reposync.timer"
  log "Check status with: systemctl status veeam-vsa-reposync.timer"
  log "Sync logs available at: ${REPOSYNC_LOGFILE}"
  log "You need to run initial repo-sync (this will download ~30 GB on first run) with:"
  log "${REPOSYNC_SCRIPT}"
  log "============================================================"
}

main "$@"
