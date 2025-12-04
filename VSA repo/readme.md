# Veeam Software Appliance Linux Repository Mirror Creation Script
# Supported OS: RHEL / Rocky / Alma / CentOS Stream 9+
# WARNING! This script is NOT officially provided by Veeam Software Corporation or is supported by Veeam in any way.
# Strictly provided AS IS - use ONLY at your own discretion

## Project Overview

This project automates the setup of a local Veeam VSA (Software Appliance) package mirror on RHEL-family Linux systems. The setup script (`vsa_repo.sh`) handles disk/filesystem prep, package management, nginx publishing, optional HTTPS, and systemd automation.

### Architecture Overview

- **Setup script** (`vsa_repo.sh`): Orchestrates the entire mirror infrastructure and runtime prompts
- **Reposync script** (generated at `/usr/local/sbin/veeam-vsa-reposync.sh`): Dynamically configured with parameters from the setup script; runs on-demand or via the systemd timer
- **Nginx** (HTTP + optional HTTPS): Publishes the local mirror, redirects HTTP to HTTPS when certs are enabled, and exposes ACME challenges
- **XFS filesystem**: Optional - will partition and mount an empty dedicated data disk (by default `/dev/sdb` -> `/mnt/data`) for repository storage (~30GB initially required)

### Execution Flow

1. **Prerequisite checks**: Verify root privileges, OS compatibility (RHEL/Rocky/Alma/CentOS 9+), and dnf availability.
3. **Cleanup path**: When selected, remove all configs, services, timers, SELinux labels, and firewall rules created by earlier runs while leaving mirrored data intact.
4. **Disk setup**: Partition and format `DATA_DEVICE` as XFS when approved; otherwise only ensure `MOUNT_POINT` exists. Includes partition table synchronization with retry logic.
5. **Package installation**: Install required packages (dnf-plugins-core, curl, nginx, SELinux tools, firewalld, gnupg2). Certbot components install only if HTTPS is chosen.
6. **Dynamic GPG key validation**: Fetch all available signing keys from `https://repository.veeam.com/keys/`, validate each via `rpm --import`, and fail if no valid keys are found.
7. **Upstream repo config**: Create `/etc/yum.repos.d/veeam-vsa-upstream.repo` with the three repo IDs and validated GPG key URLs.
8. **Reposync script generation**: Generate `/usr/local/sbin/veeam-vsa-reposync.sh` with injected configuration variables (REPO_ROOT, upstream URLs, metadata signature verification logic).
9. **Network/security**: Configure nginx (HTTP baseline, HTTPS redirect when enabled), SELinux contexts (`httpd_sys_content_t`), and firewalld rules (HTTP always, HTTPS when enabled).
10. **HTTPS provisioning**: Validate DNS for the supplied FQDN, request/renew Let's Encrypt certificates via webroot, and reload nginx with TLS settings (will reuse if existing certificates are accessible on the host)
11. **Service automation**: Create the systemd service and hourly timer that run the reposync script with automated verification.
12. **Disk space and connectivity checks**: Warn if free space is low and curl the published URLs (HTTP plus HTTPS when active).

### Runtime prompts

1. **Full cleanup**: Removes configs, scripts, services, timers, SELinux labels, and firewall rules created by this script. Does not delete repository data, installed packages, or disk partitions.
2. **Disk partitioning**: Controls whether the script partitions `DATA_DEVICE` and writes `/etc/fstab`. Pick "no" when reusing an existing path or custom storage.
3. **HTTPS enable**: Adds Let's Encrypt provisioning, port 443 listener, and HTTPS firewall rules. Requires a public FQDN that resolves to `REPO_HOST_IP`.

Answer "no" to skip any workflow while continuing with the rest of the setup.

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
- **Keys**: Dynamically fetched and validated from `https://repository.veeam.com/keys/` during setup
  - All non-DEB keys are downloaded and validated via `rpm --import`
  - Invalid or failed keys are skipped with warnings
  - At least one valid key must be imported or setup fails

### Repository Integrity Verification (Simplified in v3)
After each sync, the reposync script performs metadata signature verification:

1. **Metadata Signature Verification**
   - Downloads `repomd.xml.asc` and `repomd.xml.key` from upstream
   - Imports the signing key into a dedicated GPG keyring
   - Verifies GPG signature on `repomd.xml` using imported Veeam keys
   - Uses `gpg --batch --no-tty` to prevent hanging in automated runs
   - Filters output to show only critical verification messages

2. **Automatic Corruption Recovery**
   - **dnf reposync built-in**: Automatically detects and re-downloads corrupted or incomplete packages
   - **Metadata verification failure**: Takes mirror offline (stops nginx) if signature verification fails
   - **Manual investigation**: Required if persistent metadata corruption detected

### Configuration Variables
Edit the top section (lines 28-61) to customize:
- `DATA_DEVICE`: Physical disk (default `/dev/sdb`)
- `DATA_PARTITION`: Resulting partition (default `/dev/sdb1`)
- `MOUNT_POINT`: Where to mount filesystem (default `/mnt/data`)
- `REPO_ROOT`: Full path to repo root (derived from MOUNT_POINT)
- `OS_VERSION`, `VBR_VERSION`: Repo versions (currently 9.2 / 13.0)
- `REPO_HOSTNAME_FQDN`, `REPO_HOST_IP`: Nginx serving addresses
- `LE_EMAIL`: Email address for Let's Encrypt certificate expiry notifications

Upstream URLs are **derived from OS/VBR versions** and **injected into the generated reposync script**, so changing these auto-updates all upstream paths without regenerating the script.

### HTTPS mode
- Certificates are requested with `certbot certonly --webroot` using `${LE_WEBROOT}` and are stored under `/etc/letsencrypt/live/<FQDN>/`.
- Let's Encrypt sends expiry notifications to the configured `LE_EMAIL` address.
- Nginx always listens on port 80. When HTTPS is enabled it redirects traffic to port 443 and reuses the same repo root.
- Firewalld opens the HTTPS service automatically when TLS is in use.
- Existing certificates are reused when the script is rerun with the same FQDN.

### Monitor Sync Status
```bash
# Check timer status and next run time
systemctl status veeam-vsa-reposync.timer
systemctl list-timers veeam-vsa-reposync.timer

# View recent sync logs (includes integrity verification results)
journalctl -u veeam-vsa-reposync.service -n 100

# Watch live sync progress
journalctl -u veeam-vsa-reposync.service -f
```

### Verify Mirror Integrity
```bash
# Manual verification run
sudo /usr/local/sbin/veeam-vsa-reposync.sh

# Check verification output (simplified in v3)
# Look for:
#   ✓ SUCCESS: Metadata signature verified for <repo>
#   ✓ ALL REPOSITORY METADATA SIGNATURES VERIFIED SUCCESSFULLY
```

### Verify Mirror Availability
```bash
curl -I http://veeamrepo/vsa/9.2/vbr/13.0/mandatory/
curl -I http://192.168.1.54/vsa/9.2/vbr/13.0/optional/
curl -I http://veeamrepo.test.local/vsa/9.2/external-mandatory/
curl -I https://veeamrepo.test.local/vsa/9.2/vbr/13.0/mandatory/   # only when HTTPS is enabled
```

### Integration Points
- **Clients**: Point VSA installations to `http://<REPO_HOST_IP>/vsa/...` (or `https://<FQDN>/vsa/...` when TLS is enabled)

### Sync Frequency

The default sync schedule is **hourly**
To change the sync interval, edit the timer file:
```bash
/etc/systemd/system/veeam-vsa-reposync.timer
```
Modify the `OnCalendar=` line. Examples:
```ini
# Every hour (default)
OnCalendar=hourly

# Every 6 hours
OnCalendar=*-*-* 00/6:00:00

# Daily at 3 AM
OnCalendar=*-*-* 03:00:00
```

Then reload and restart the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl restart veeam-vsa-reposync.timer

# Verify next scheduled run
systemctl list-timers | grep veeam
```

### Reposync Logging
The reposync script logs all activity to a persistent log file with automatic rotation
Each sync tracks and logs package changes per repository

- **Log file location**: `/var/log/veeam-vsa-reposync.log`
- **Dual output**: All messages go to both stderr (for journalctl) and the log file
- **Added packages**: New RPMs downloaded during sync
- **Removed packages**: Old RPMs deleted by `--delete` flag

### Log Rotation
Logrotate configuration is automatically created at `/etc/logrotate.d/veeam-vsa-reposync`:
- **Rotation**: Monthly
- **Retention**: 3 months (rotate 3)
- **Compression**: Enabled with delayed compression
- **Permissions**: 0640 root:root

### Logrotate Commands
```bash
# Test logrotate configuration (dry-run / debug mode)
logrotate -d /etc/logrotate.d/veeam-vsa-reposync

# Force immediate rotation
logrotate -f /etc/logrotate.d/veeam-vsa-reposync

# Search for package changes
grep -E '\[Added\]|\[Removed\]' /var/log/veeam-vsa-reposync.log
```

## ✍ Contributions

We welcome contributions from the community! We encourage you to create [issues](https://github.com/VeeamHub/veeam-vsa-repo-mirror/issues/new/choose) for Bugs & Feature Requests and submit Pull Requests. For more detailed information, refer to our [Contributing Guide](CONTRIBUTING.md).

## 🤝🏾 License

* [MIT License](LICENSE)

## 🤔 Questions

If you have any questions or something is unclear, please don't hesitate to [create an issue](https://github.com/VeeamHub/veeam-vsa-repo-mirror/issues/new/choose) and let us know!


