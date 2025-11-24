# Veeam VSA Linux Repository Mirror creation script
# Supported OS: RHEL / Rocky / Alma / CentOS Stream 9+

## Project Overview

This project automates the setup of a **local Veeam VSA (Software Appliance) package mirror** on RHEL-family Linux systems. The setup script performs a comprehensive initialization of disk/filesystem, package management, nginx publishing, and systemd automation.

### Architecture Overview

- **Setup script** (`vsa_repo.sh`): Orchestrates the entire mirror infrastructure setup
- **Reposync script** (generated at `/usr/local/sbin/veeam-vsa-reposync.sh`): Dynamically configured with parameters from setup script; runs on-demand or hourly via systemd timer to mirror RPM packages from upstream Veeam repositories
- **Nginx** (port 80): Publishes the local mirror for client consumption
- **XFS filesystem**: Optional: Dedicated data disk (`/dev/sdb` → `/mnt/data`) for repository storage (~30GB initial)

### Execution Flow

1. **Prerequisite checks**: Verify root privileges, OS compatibility (RHEL/Rocky/Alma/CentOS 9+), dnf availability
2. **Disk setup**: Partitions `/dev/sdb` as XFS if not already done and system detects it's empty; skips if partition exists or disk not empty, for example when local folder is used and not dedicated drive.
3. **Package installation**: checks and installs requred packages (dnf-plugins-core, curl, nginx, SELinux, firewalld)
4. **Upstream repo config**: Creates `/etc/yum.repos.d/veeam-vsa-upstream.repo` (defines 3 repo IDs)
5. **Reposync script generation**: Generates `/usr/local/sbin/veeam-vsa-reposync.sh` with injected configuration variables (REPO_ROOT, upstream URLs)
6. **Directory setup**: Creates repo root with proper ownership and permissions
7. **Network/security**: Configures nginx, SELinux contexts (`httpd_sys_content_t`), firewalld rules (allowing HTTP traffic incoming)
8. **Service automation**: Creates systemd `.service` + `.timer` for hourly sync
9. **Disk space validation**: Checks available space on mount point before sync operations
10. **Connectivity verification**: Tests local HTTP access to mirror paths for sanity check which proves it is being served correctly locally

## Key Conventions

### Repo Structure Hierarchy
```
/mnt/data/repo/repository.veeam.com/
  ├── vsa/9.2/vbr/13.0/mandatory/
  ├── vsa/9.2/vbr/13.0/optional/
  └── vsa/9.2/external-mandatory/
```
This mirrors the **upstream path structure** exactly. Config variables define OS_VERSION (9.2) and VBR_VERSION (13.0).

### GPG Handling Strategy
- **Mirror host**: `--nogpgcheck` enabled in `dnf reposync` because `external-mandatory` contains packages from multiple vendors (Rocky, CIQ, PGDG, etc.)
- **VSA clients**: Still perform full GPG verification when consuming this mirror
- Keys synced via `sync_veeam_keys()` from `https://repository.veeam.com/keys/`

### Configuration Variables
Edit the top section (lines 28-57) to customize:
- `DATA_DEVICE`: Physical disk (default `/dev/sdb`)
- `DATA_PARTITION`: Resulting partition (default `/dev/sdb1`)
- `MOUNT_POINT`: Where to mount filesystem (default `/mnt/data`)
- `REPO_ROOT`: Full path to repo root (derived from MOUNT_POINT)
- `OS_VERSION`, `VBR_VERSION`: Repo versions (currently 9.2 / 13.0)
- `REPO_HOSTNAME_FQDN`, `REPO_HOST_IP`: Nginx serving addresses

Upstream URLs are **derived from OS/VBR versions** and **injected into the generated reposync script**, so changing these auto-updates all upstream paths without regenerating the script.

### Monitor Sync Status
```bash
systemctl status veeam-vsa-reposync.timer
systemctl list-timers veeam-vsa-reposync.timer
journalctl -u veeam-vsa-reposync.service -n 50
```

### Verify Mirror Availability
```bash
curl -I http://veeamrepo/vsa/9.2/vbr/13.0/mandatory/
curl -I http://192.168.1.54/vsa/9.2/vbr/13.0/optional/
curl -I http://veeamrepo.test.local/vsa/9.2/external-mandatory/
```

### Integration Points
- **Clients**: Redirect VSA installations to `http://<REPO_HOST_IP>/vsa/...`

### Sync Frequency
To change sync interval (default 1 hour):
1. Edit `OnUnitActiveSec=1h` in generated systemd unit file
3. Reload systemd: `systemctl daemon-reload`

