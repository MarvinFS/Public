#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Veeam Software Appliance repository mirror script 
#
# This script orchestrates the complete setup of a local Veeam VSA
# package mirror infrastructure on RHEL-family systems. It:
#
#   1) Partitions and mounts a dedicated XFS filesystem on extra data disk
#   2) Installs core packages (dnf-plugins-core, curl, nginx, SELinux tools, firewalld)
#   3) Creates /etc/yum.repos.d/veeam-vsa-upstream.repo with 3 repo definitions
#   4) Generates parameterized /usr/local/sbin/veeam-vsa-reposync.sh with injected configuration
#   5) Sets up repo directory with proper ownership and permissions
#   6) Configures nginx to serve mirror on port 80
#   7) Applies SELinux contexts and firewall rules
#   8) Creates systemd service + hourly timer for automated syncing
#   9) Validates available disk space for sync operations
#   10) Tests local HTTP connectivity to mirror paths
#
# NOTE: This script sets up infrastructure only. Manual reposync trigger required:
#   sudo /usr/local/sbin/veeam-vsa-reposync.sh
#
# Supported: RHEL / Rocky / Alma / CentOS Stream 9+ (incl. Rocky 10)
# NOT SUPPORTED: Debian / Ubuntu / SUSE / anything without dnf + reposync.
#
# Created by MarvinFS wrapped up with AI assistance.
# This script is NOT officially provided by Veeam Software company or supported in any way.
# Strictly provided AS IS - use at your own discretion
# ============================================================

# -------------------------
# CONFIG - EDIT IF NEEDED
# -------------------------
DATA_DEVICE="/dev/sdb"              # disk to use for repo data (will be partitioned) I have added new drive to a VM 100GB
DATA_PARTITION="${DATA_DEVICE}1"    # resulting partition 
MOUNT_POINT="/mnt/data"             # mount point for XFS

REPO_ROOT="${MOUNT_POINT}/repo/repository.veeam.com"

OS_VERSION="9.2"
VBR_VERSION="13.0"

# desired hostnames and IP which NGINX serves on port 80 (no SSL used, if needed, use any reverse proxy with SSL certs)
REPO_HOSTNAME_SHORT="veeamrepo"
REPO_HOSTNAME_FQDN="veeamrepo.test.local"
REPO_HOST_IP="192.168.1.54"

# Paths and constants
UPSTREAM_MANDATORY_URL="https://repository.veeam.com/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory"
UPSTREAM_OPTIONAL_URL="https://repository.veeam.com/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/optional"
UPSTREAM_EXTERNAL_MANDATORY_URL="https://repository.veeam.com/vsa/${OS_VERSION}/external-mandatory"

DNF_BIN="/usr/bin/dnf"
KEY_INDEX_URL="https://repository.veeam.com/keys/"
KEY_LOCAL_DIR="/etc/veeam/rpm-gpg"
UPSTREAM_REPO_FILE="/etc/yum.repos.d/veeam-vsa-upstream.repo"
REPOSYNC_SCRIPT="/usr/local/sbin/veeam-vsa-reposync.sh"
NGINX_CONF="/etc/nginx/conf.d/veeam-repo.conf"

REPOID_MANDATORY="veeam-vsa-mandatory"
REPOID_OPTIONAL="veeam-vsa-optional"
REPOID_EXTERNAL_MANDATORY="veeam-vsa-external-mandatory"

SYSTEMD_SERVICE="/etc/systemd/system/veeam-vsa-reposync.service"
SYSTEMD_TIMER="/etc/systemd/system/veeam-vsa-reposync.timer"

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

# -------------------------
# BASIC CHECKS
# -------------------------
require_root() {
  if [[ $EUID -ne 0 ]]; then
    fatal "Must be run as root."
  fi
}

check_os() {
  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
  else
    fatal "/etc/os-release not found. Unsupported OS."
  fi

  case "${ID:-unknown}" in
    rhel|rocky|almalinux|centos)
      log "OS detected: ${PRETTY_NAME:-$ID}"
      ;;
    *)
      fatal "Unsupported OS ID: ${ID:-unknown}. Only RHEL/Rocky/Alma/CentOS-family is supported."
      ;;
  esac
}

check_dnf() {
  if ! command -v "${DNF_BIN}" >/dev/null 2>&1; then
    fatal "dnf not found. This script requires a dnf based system."
  fi
}

# -------------------------
# DISK AND FILESYSTEM SETUP
# -------------------------
prepare_disk() {
  log "Checking data disk ${DATA_DEVICE}"

  if [[ ! -b "${DATA_DEVICE}" ]]; then
    fatal "Device ${DATA_DEVICE} does not exist. Adjust DATA_DEVICE in script."
  fi

  # If partition already exists and is XFS, assume it is fine and skip destructive steps
  if lsblk -no TYPE "${DATA_PARTITION}" 2>/dev/null | grep -q '^part$'; then
    local fstype
    fstype=$(lsblk -no FSTYPE "${DATA_PARTITION}" 2>/dev/null || true)
    log "Found existing partition ${DATA_PARTITION} (FSTYPE=${fstype:-unknown}). Will NOT repartition or reformat."

    if [[ "${fstype}" != "xfs" ]]; then
      log "WARNING: ${DATA_PARTITION} is not XFS. Script expects XFS. Mount may fail."
    fi
    return
  fi

  log "No existing partition ${DATA_PARTITION} detected. Creating fresh GPT + XFS."

  # Extra sanity: ensure disk is not already mounted
  if lsblk -no MOUNTPOINT "${DATA_DEVICE}" | grep -q '/'; then
    fatal "${DATA_DEVICE} appears to have mounted filesystems. Refusing to wipe."
  fi

  # Wipe signatures and create partition table
  log "Wiping filesystem signatures on ${DATA_DEVICE}"
  wipefs -a "${DATA_DEVICE}"

  log "Creating GPT and single XFS partition on ${DATA_DEVICE}"
  parted "${DATA_DEVICE}" --script \
    mklabel gpt \
    mkpart primary xfs 0% 100%

  # Format partition
  log "Formatting ${DATA_PARTITION} as XFS"
  mkfs.xfs -f "${DATA_PARTITION}"

  log "Disk preparation completed for ${DATA_PARTITION}"
}

ensure_mountpoint() {
  mkdir -p "${MOUNT_POINT}"
  chmod 0755 "${MOUNT_POINT}"
}

ensure_fstab_entry() {
  log "Ensuring /etc/fstab has PARTUUID entry for ${DATA_PARTITION}"

  local partuuid
  partuuid=$(blkid -o value -s PARTUUID "${DATA_PARTITION}" 2>/dev/null || true)
  if [[ -z "${partuuid}" ]]; then
    fatal "Could not read PARTUUID for ${DATA_PARTITION}. Check blkid output."
  fi

  local fstab_line="PARTUUID=${partuuid}   ${MOUNT_POINT}   xfs   defaults,noatime   0 0"

  if grep -q "PARTUUID=${partuuid}" /etc/fstab 2>/dev/null; then
    log "fstab already contains entry for PARTUUID=${partuuid}. Skipping append."
  else
    log "Adding new fstab entry: ${fstab_line}"
    if ! printf "\n%s\n" "${fstab_line}" >> /etc/fstab; then
      fatal "Failed to write to /etc/fstab. Check permissions and disk space."
    fi
  fi
}

mount_data_fs() {
  log "Reloading systemd daemon and mounting all filesystems."
  systemctl daemon-reload || true
  mount -a

  local mp
  mp=$(lsblk -no MOUNTPOINT "${DATA_PARTITION}" 2>/dev/null || true)
  if [[ "${mp}" != "${MOUNT_POINT}" ]]; then
    fatal "Expected ${DATA_PARTITION} to be mounted on ${MOUNT_POINT} but got '${mp:-<none>}'"
  fi

  log "Mount check passed: ${DATA_PARTITION} -> ${MOUNT_POINT}"
}

# -------------------------
# CORE PACKAGES
# -------------------------
install_core_packages() {
  log "Installing required packages (dnf-plugins-core, curl, nginx, SELinux tools, firewalld helpers)"

  "${DNF_BIN}" install -y dnf-plugins-core curl nginx policycoreutils-python-utils selinux-policy-targeted firewalld || \
    log "WARNING: Some packages failed to install. If your distro variant does not use them, this may be ok."

  # Make sure dnf reposync is available
  if ! "${DNF_BIN}" --help 2>&1 | grep -q 'reposync'; then
    fatal "dnf reposync plugin not available even after installing dnf-plugins-core."
  fi
}

# -------------------------
# DISK SPACE VALIDATION
# -------------------------
check_disk_space() {
  log "Checking available disk space on ${MOUNT_POINT}"

  local available_gb
  available_gb=$(df -B1G "${MOUNT_POINT}" | awk 'NR==2 {print $4}' | sed 's/G$//')

  if [[ -z "${available_gb}" ]]; then
    log "WARNING: Could not determine available disk space. Proceeding anyway."
    return
  fi

  # Warn if less than 40GB available (initial sync is ~30GB + buffer)
  if (( available_gb < 40 )); then
    log "WARNING: Only ${available_gb}GB available on ${MOUNT_POINT}. Initial reposync needs ~30GB."
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
    log "Upstream repo file ${UPSTREAM_REPO_FILE} already exists. Leaving it intact."
    return
  fi

  log "Creating upstream repo definition at ${UPSTREAM_REPO_FILE}"

  tee "${UPSTREAM_REPO_FILE}" >/dev/null <<EOF
[${REPOID_MANDATORY}]
name=Veeam VSA mandatory (upstream mirror source)
baseurl=https://repository.veeam.com/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory/
enabled=0
gpgcheck=0
repo_gpgcheck=0
gpgkey=https://repository.veeam.com/keys/RPM-E6FBD664 https://repository.veeam.com/keys/RPM-EFDCEA77

[${REPOID_OPTIONAL}]
name=Veeam VSA optional (upstream mirror source)
baseurl=https://repository.veeam.com/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/optional/
enabled=0
gpgcheck=0
repo_gpgcheck=0
gpgkey=https://repository.veeam.com/keys/RPM-E6FBD664 https://repository.veeam.com/keys/RPM-EFDCEA77

[${REPOID_EXTERNAL_MANDATORY}]
name=Veeam VSA external mandatory (upstream mirror source)
baseurl=https://repository.veeam.com/vsa/${OS_VERSION}/external-mandatory/
enabled=0
gpgcheck=0
repo_gpgcheck=0
gpgkey=https://repository.veeam.com/keys/RPM-E6FBD664 https://repository.veeam.com/keys/RPM-EFDCEA77
EOF
}

# -------------------------
# REPOSYNC SCRIPT
# -------------------------
create_reposync_script() {
  log "Creating reposync script at ${REPOSYNC_SCRIPT}"

  mkdir -p "$(dirname "${REPOSYNC_SCRIPT}")"

  # Inject configuration variables into the embedded script
  tee "${REPOSYNC_SCRIPT}" >/dev/null <<EOF
#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# VSA local mirror script using dnf reposync
# Supported: RHEL / Rocky / Alma / CentOS Stream 9+ (incl. Rocky 10)
# NOT SUPPORTED: Debian/Ubuntu or non-dnf systems.
# Created by MarvinFS 
# This script is NOT officially provided by Veeam Software company or supported in any way.
# Strictly provided AS IS - use at your own discretion
#
# NOTE:
#  - GPG checks are DISABLED on the mirror host (--nogpgcheck),
#    because external-mandatory contains packages signed by
#    multiple vendor keys (Rocky, CIQ, PGDG, etc).
#  - The VSA itself can still enforce full GPG checks (packages
#    and metadata) when using this mirror.
# ============================================================

REPO_ROOT="${REPO_ROOT}"

REPOID_MANDATORY="veeam-vsa-mandatory"
REPOID_OPTIONAL="veeam-vsa-optional"
REPOID_EXTERNAL_MANDATORY="veeam-vsa-external-mandatory"

# Upstream Veeam URLs for VSA repos (metadata signatures live here)
UPSTREAM_MANDATORY_URL="${UPSTREAM_MANDATORY_URL}"
UPSTREAM_OPTIONAL_URL="${UPSTREAM_OPTIONAL_URL}"
UPSTREAM_EXTERNAL_MANDATORY_URL="${UPSTREAM_EXTERNAL_MANDATORY_URL}"

DNF_BIN="/usr/bin/dnf"
KEY_INDEX_URL="https://repository.veeam.com/keys/"
KEY_LOCAL_DIR="/etc/veeam/rpm-gpg"

log() {
  printf '[%(%Y-%m-%d %H:%M:%S)T] %s\n' -1 "$*" >&2
}

check_os() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
  else
    log "ERROR: /etc/os-release not found. Unsupported OS."
    exit 1
  fi

  case "${ID:-unknown}" in
    rhel|rocky|almalinux|centos)
      ;;
    *)
      log "ERROR: This script supports only RHEL/Rocky/Alma/CentOS-family systems."
      exit 1
      ;;
  esac
}

check_prereqs() {
  if ! command -v "${DNF_BIN}" >/dev/null 2>&1; then
    log "ERROR: dnf not found. This is not a RHEL-family system."
    exit 1
  fi

  if ! "${DNF_BIN}" --help 2>&1 | grep -q 'reposync'; then
    log "ERROR: dnf reposync plugin not available. Install dnf-plugins-core first:"
    log "       dnf install -y dnf-plugins-core"
    exit 1
  fi

  if ! command -v curl >/dev/null 2>&1; then
    log "ERROR: curl not found. Install it first:"
    log "       dnf install -y curl"
    exit 1
  fi

  if [[ ! -f /etc/yum.repos.d/veeam-vsa-upstream.repo ]]; then
    log "ERROR: /etc/yum.repos.d/veeam-vsa-upstream.repo not found."
    log "       Create it before running this script."
    exit 1
  fi
}

sync_veeam_keys() {
  log "Syncing Veeam RPM GPG keys from ${KEY_INDEX_URL}"

  mkdir -p "${KEY_LOCAL_DIR}"

  local index
  if ! index=$(curl -fsSL "${KEY_INDEX_URL}"); then
    log "WARNING: Failed to download key index, skipping key sync."
    return
  fi

  local keys
  keys=$(printf '%s\n' "${index}" | grep -o 'RPM-[A-Za-z0-9]\+' | sort -u || true)

  if [[ -z "${keys}" ]]; then
    log "WARNING: No RPM-* keys found in index, skipping key sync."
    return
  fi

  local k
  for k in ${keys}; do
    local dst="${KEY_LOCAL_DIR}/${k}.gpg"
    local url="${KEY_INDEX_URL}${k}"

    log "  - Downloading key ${k}"
    if curl -fsSL "${url}" -o "${dst}"; then
      rpm --import "${dst}" || log "WARNING: Failed to import key ${dst}"
    else
      log "WARNING: Could not download key ${k}"
    fi
  done

  log "Veeam RPM GPG key sync completed (some keys may be rejected by policies - this is expected)."
}

sync_repo_signatures() {
  local upstream_url="$1"
  local target_path="$2"
  local repodata_path="${target_path}/repodata"

  mkdir -p "${repodata_path}"

  # We try to pull both repomd.xml.asc and repomd.xml.key.
  # If they are missing upstream, we only log a warning and continue.
  local f
  for f in repomd.xml.asc repomd.xml.key; do
    local url="${upstream_url}/repodata/${f}"
    local dst="${repodata_path}/${f}"

    log "Syncing metadata signature ${f} from ${url}"
    if curl -fsSL "${url}" -o "${dst}"; then
      log "  - Downloaded ${f} to ${dst}"
    else
      log "WARNING: Could not download ${f} from ${url} (metadata GPG for this repo may fail if repo_gpgcheck=1)."
    fi
  done
}

mirror_repo() {
  local repoid="$1"
  local relpath="$2"
  local upstream_url="$3"
  local target_path="${REPO_ROOT}/${relpath}"

  mkdir -p "${target_path}"

  log "Running dnf reposync for ${repoid}"
  log "Target: ${target_path}"

  # IMPORTANT:
  #   --nogpgcheck is ONLY for this mirror host.
  #   The VSA still does GPG checks when consuming this mirror.
  "${DNF_BIN}" -y reposync \
    --repoid="${repoid}" \
    --download-metadata \
    --download-path="${target_path}" \
    --norepopath \
    --delete \
    --nogpgcheck

  # After successful reposync, pull metadata signature files
  sync_repo_signatures "${upstream_url}" "${target_path}"
}

main() {
  if [[ $EUID -ne 0 ]]; then
    log "ERROR: Must run as root."
    exit 1
  fi

  check_os
  check_prereqs
  sync_veeam_keys

  # Mirror the three VSA repos into the expected tree
  mirror_repo "${REPOID_MANDATORY}"          "vsa/9.2/vbr/13.0/mandatory"       "${UPSTREAM_MANDATORY_URL}"
  mirror_repo "${REPOID_OPTIONAL}"           "vsa/9.2/vbr/13.0/optional"        "${UPSTREAM_OPTIONAL_URL}"
  mirror_repo "${REPOID_EXTERNAL_MANDATORY}" "vsa/9.2/external-mandatory"       "${UPSTREAM_EXTERNAL_MANDATORY_URL}"

  log "Veeam VSA reposync completed successfully."
}

main "$@"
EOF

  chmod 0755 "${REPOSYNC_SCRIPT}"
EOF
}

# -------------------------
# NGINX CONFIG
# -------------------------
configure_nginx() {
  log "Enabling and starting nginx"
  systemctl enable --now nginx || fatal "Failed to enable or start nginx."

  log "Writing nginx repo config to ${NGINX_CONF}"

  tee "${NGINX_CONF}" >/dev/null <<EOF
server {
    listen 80;
    server_name ${REPO_HOSTNAME_FQDN} ${REPO_HOSTNAME_SHORT} ${REPO_HOST_IP};

    # Root of the mirrored repo
    root ${REPO_ROOT};

    autoindex on;
    autoindex_exact_size off;
    autoindex_localtime on;

    # Everything served read-only
    location / {
        try_files \$uri \$uri/ =404;
    }
}
EOF

  log "Testing nginx configuration"
  nginx -t

  log "Reloading nginx"
  systemctl reload nginx
}

# -------------------------
# SELINUX CONTEXTS
# -------------------------
configure_selinux() {
  if command -v getenforce >/dev/null 2>&1; then
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
  else
    log "getenforce not available. Assuming SELinux not in use, skipping."
  fi
}

# -------------------------
# FIREWALLD
# -------------------------
configure_firewall() {
  if systemctl is-active --quiet firewalld; then
    log "Opening HTTP service in firewalld"
    firewall-cmd --add-service=http --permanent || \
      log "WARNING: failed to add http service to firewalld."
    firewall-cmd --reload || \
      log "WARNING: failed to reload firewalld."
  else
    log "firewalld is not active. Skipping firewall configuration."
  fi
}

# -------------------------
# CONNECTIVITY CHECKS
# -------------------------
check_connectivity() {
  if ! command -v curl >/dev/null 2>&1; then
    log "curl not available for connectivity checks. Skipping."
    return
  fi

  log "Checking local HTTP access to repo paths"

  local urls=(
    "http://${REPO_HOSTNAME_SHORT}/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory/"
    "http://${REPO_HOSTNAME_FQDN}/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory/"
    "http://${REPO_HOST_IP}/vsa/${OS_VERSION}/vbr/${VBR_VERSION}/mandatory/"
  )

  local u
  for u in "${urls[@]}"; do
    log "  - curl -I ${u}"
    if curl -I -s "${u}" >/dev/null; then
      log "    OK: ${u}"
    else
      log "    WARNING: failed to connect to ${u}"
    fi
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
OnBootSec=15min
OnUnitActiveSec=1h
Unit=veeam-vsa-reposync.service
Persistent=true

[Install]
WantedBy=timers.target
EOF

  log "Reloading systemd units and enabling timer"
  systemctl daemon-reload
  systemctl enable --now veeam-vsa-reposync.timer
}

# -------------------------
# MAIN
# -------------------------
main() {
  require_root
  check_os
  check_dnf

  # 1) Disk and filesystem setup
  prepare_disk
  ensure_mountpoint
  ensure_fstab_entry
  mount_data_fs

  # 2) Package installation
  install_core_packages

  # 3) Upstream repo configuration
  create_upstream_repo_file

  # 4) Reposync script generation (with injected configuration)
  create_reposync_script

  # 5) Directory preparation
  log "Ensuring repo root directory ${REPO_ROOT}"
  mkdir -p "${REPO_ROOT}"
  chown root:root "${MOUNT_POINT}" "${MOUNT_POINT}/repo" "${REPO_ROOT}" || true
  chmod 0755 "${MOUNT_POINT}" "${MOUNT_POINT}/repo" "${REPO_ROOT}" || true

  # 6) Network and security configuration
  configure_nginx
  configure_selinux
  configure_firewall

  # 7) Systemd automation setup
  create_systemd_units

  # 8) Connectivity test
  check_connectivity

  log "============================================================"
  log "Veeam VSA mirror setup completed."
  log "Serve URL examples:"
  log "  http://${REPO_HOSTNAME_SHORT}/vsa"
  log "  http://${REPO_HOSTNAME_FQDN}/vsa"
  log "  http://${REPO_HOST_IP}/vsa"
  log ""
  log "Hourly sync is handled by systemd timer: veeam-vsa-reposync.timer"
  log "Check status with: systemctl status veeam-vsa-reposync.timer"
  log "You need to run initial repo-sync (this will download ~30 GB on first run) with:"
  log "${REPOSYNC_SCRIPT}"
  log "============================================================"
}

main "$@"
