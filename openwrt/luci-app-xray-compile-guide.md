# Complete Guide: Compiling luci-app-xray for OpenWrt

This guide walks you through compiling `luci-app-xray` packages for OpenWrt routers using the OpenWrt SDK on Debian 12.

---

## 🎯 This Guide Works for ANY OpenWrt Router!

**Important:** As an example, for this guide relatively powerful **GL.iNet GL-MT6000** (OpenWrt 24.10.5, mediatek/filogic, aarch64_cortex-a53) was used, but the process is identical for:
- Xiaomi routers (AX3600, Mi Router 3G, etc.)
- TP-Link routers (Archer C7, WDR4300, etc.)
- Netgear routers (R7800, R6220, etc.)
- Any OpenWrt-supported device

**You just need to replace the example values with YOUR router's specifications:**
- Version: `24.10.5` → YOUR OpenWrt version
- Target: `mediatek/filogic` → YOUR target/subtarget
- Architecture: `aarch64_cortex-a53` → YOUR architecture

---

## ⚠️ Important Requirements

- **Use Debian 12 (Bookworm)** - This proven to be rock solid in compiling this app,Ubuntu 24.04 misewarably failed with numerous dependency issues, and Debian 13 is untested. (NOTE: You can't compile the app on the router directly, that is not possible!) 
- **Compilation time**: ~2 hours on a decent machine
- **Disk space**: ~7.5GB total
  - Minimal Debian 12 install: ~800MB
  - SDK + packages + build artifacts: ~6.7GB
  - Recommend >10GB partition to be safe
- **Alternative**: Consider using GitHub Actions for automated builds (see original repository README at https://github.com/yichya/luci-app-xray)

---

## What You'll Get

After compilation, you'll have 4 IPK files:
1. `xray-core_*.ipk` - The main Xray proxy binary
2. `xray-example_*.ipk` - Example configuration files
3. `luci-app-xray_*.ipk` - Web interface for Xray
4. `luci-app-xray-status_*.ipk` - Status page for the web interface

---

## Part 1: Identify Your Router's Specifications

**Do this FIRST - you need this information before starting compilation!**

### Step 1: Get Router Information

You need to know your router's exact specifications:

1. **Router Model** (e.g., GL-MT6000, Xiaomi AX3600, TP-Link Archer C7)
2. **OpenWrt Version Installed** (e.g., 24.10.5, 23.05.3, 22.03.5)
3. **Target/Subtarget** (e.g., mediatek/filogic, ramips/mt7621, ath79/generic)
4. **Architecture** (e.g., aarch64_cortex-a53, mipsel_24kc, arm_cortex-a7)

### Method 1: Check on Your Router (Most Reliable)

```bash
# SSH into your router
# Get all information at once
cat /etc/openwrt_release
```

**Example output (GL.iNet GL-MT6000 Flint 2):**
```
DISTRIB_ID='OpenWrt'
DISTRIB_RELEASE='24.10.5'
DISTRIB_REVISION='r28259-ff0d8c181b'
DISTRIB_TARGET='mediatek/filogic'
DISTRIB_ARCH='aarch64_cortex-a53'
DISTRIB_DESCRIPTION='OpenWrt 24.10.5 r28259-ff0d8c181b'
```

From this output:
- **Version**: 24.10.5
- **Target**: mediatek/filogic
- **Architecture**: aarch64_cortex-a53

### Method 2: Check OpenWrt Device Database

If you haven't installed OpenWrt yet, or want to verify:

1. Visit: https://openwrt.org/toh/start
2. Search for your router model
3. Look at the "Target" and "CPU" columns
4. Note the architecture (usually matches CPU type)

### Common Target/Architecture Combinations

| Router Type | Common Target | Common Architecture | Examples |
|-------------|---------------|---------------------|----------|
| MediaTek MT7621 | ramips/mt7621 | mipsel_24kc | Xiaomi Mi Router 3G, Netgear R6220 |
| MediaTek Filogic | mediatek/filogic | aarch64_cortex-a53 | GL-MT6000, Xiaomi Redmi AX6000 |
| Qualcomm IPQ40xx | ipq40xx/generic | arm_cortex-a7_neon-vfpv4 | GL-B1300, TP-Link Archer C2600 |
| Qualcomm IPQ806x | ipq806x/generic | arm_cortex-a15_neon-vfpv4 | Netgear R7800, TP-Link Archer C2600 |
| Atheros AR71xx | ath79/generic | mips_24kc | TP-Link Archer C7 v2, WDR4300 |
| Broadcom BCM47xx | bcm47xx/generic | mipsel_mips32 | Asus RT-N16, Linksys WRT320N |
| Rockchip RK33xx | rockchip/armv8 | aarch64_generic | NanoPi R4S, R5S |
| x86/64 (VM/PC) | x86/64 | x86_64 | Virtual machines, PC builds |

**Remember:** These are examples. Always verify YOUR specific router!

---

## Part 2: Setup Build Environment

### Step 2: Install Prerequisites

```bash
# Update system
sudo apt update

# Install required packages for OpenWrt SDK
sudo apt install -y build-essential clang flex bison g++ gawk \
gcc-multilib g++-multilib gettext git libncurses5-dev libssl-dev \
python3-setuptools rsync swig unzip zlib1g-dev file wget curl zstd
```

### Step 3: Download the Correct OpenWrt SDK

**CRITICAL:** You MUST download the SDK that exactly matches:
- Your OpenWrt version (e.g., 24.10.5, 23.05.3, 22.03.5)
- Your router's target (e.g., mediatek/filogic, ramips/mt7621)

#### SDK URL Pattern

```
https://downloads.openwrt.org/releases/[VERSION]/targets/[TARGET]/[SUBTARGET]/openwrt-sdk-[VERSION]-[TARGET]-[SUBTARGET]_*.tar.[zst|xz]
```
#### Compression Formats by Version

- OpenWrt 24.x: Uses `.tar.zst` (requires `tar --zstd`)
- OpenWrt 23.x and older: Uses `.tar.xz` (requires `tar -xJf`)

#### Example 1: GL.iNet GL-MT6000 (OpenWrt 24.10.5)

```bash
cd ~

# Download SDK
wget https://downloads.openwrt.org/releases/24.10.5/targets/mediatek/filogic/openwrt-sdk-24.10.5-mediatek-filogic_gcc-13.3.0_musl.Linux-x86_64.tar.zst

# Extract (newer versions use .zst)
tar --zstd -xf openwrt-sdk-24.10.5-mediatek-filogic_gcc-13.3.0_musl.Linux-x86_64.tar.zst

# Rename for easier access
mv openwrt-sdk-24.10.5-mediatek-filogic_gcc-13.3.0_musl.Linux-x86_64 openwrt-sdk

cd openwrt-sdk
```

#### Example 2: Xiaomi Mi Router 3G (OpenWrt 23.05.3)

```bash
cd ~

# Download SDK
wget https://downloads.openwrt.org/releases/23.05.3/targets/ramips/mt7621/openwrt-sdk-23.05.3-ramips-mt7621_gcc-12.3.0_musl.Linux-x86_64.tar.xz

# Extract (older versions use .xz)
tar -xJf openwrt-sdk-23.05.3-ramips-mt7621_gcc-12.3.0_musl.Linux-x86_64.tar.xz

# Rename
mv openwrt-sdk-23.05.3-ramips-mt7621_gcc-12.3.0_musl.Linux-x86_64 openwrt-sdk

cd openwrt-sdk
```

#### GCC Versions by OpenWrt Release

- OpenWrt 24.10.x: GCC 13.3.0
- OpenWrt 23.05.x: GCC 12.3.0
- OpenWrt 22.03.x: GCC 11.2.0

**Always use the EXACT version installed on your router!** Mismatches may cause:
- Kernel version conflicts
- ABI incompatibilities
- Package installation failures

#### Verify Your SDK

After extraction, verify it matches your router:

```bash
cd ~/openwrt-sdk
cat .config | grep CONFIG_TARGET
```

Output should show your target, for example:
```
CONFIG_TARGET_mediatek=y
CONFIG_TARGET_mediatek_filogic=y
```

---

## Part 3: Configure and Build luci-app-xray

### Step 4: Add the luci-app-xray Feed

```bash
# Edit feeds configuration with any text editor
nano feeds.conf.default
```

Add this line at the end:
```
src-git-full luci_app_xray https://github.com/yichya/luci-app-xray
```

**Save and exit** (nano: Ctrl+X, then Y, then Enter)

### Step 5: Update and Install Feeds

```bash
# Update all feeds (downloads the packages)
./scripts/feeds update -a

# Install luci-app-xray packages
./scripts/feeds install luci-app-xray luci-app-xray-status

# Verify installation
./scripts/feeds list -i | grep xray
```

You should see output like:
```
xray-core
xray-example
luci-app-xray
luci-app-xray-geodata
luci-app-xray-status
```

### Step 6: Configure Build Selection (Optional)

If you want to select packages manually:
```bash
make menuconfig
```

Navigate to: **Extra Packages** → find `luci-app-xray`
- Press **M** to mark it for building as a module (shows `<M>`)
- Also mark `luci-app-xray-status` with **M**
- Save and exit (press ESC twice, confirm save)

### Step 7: Build the Packages

```bash
# Build will be performed with verbose output and will use all CPU cores
make package/feeds/luci_app_xray/luci-app-xray/compile V=s -j$(nproc)
```
### Step 8: Find Your Built Packages

```bash
cd ~/openwrt-sdk
find bin/ -name "*xray*.ipk"
```

**Expected output (paths will vary based on YOUR architecture):**
```
bin/packages/aarch64_cortex-a53/packages/xray-core_25.1.30-r1_aarch64_cortex-a53.ipk
bin/packages/aarch64_cortex-a53/packages/xray-example_25.1.30-r1_all.ipk
bin/packages/aarch64_cortex-a53/luci_app_xray/luci-app-xray_3.6.1-r1_all.ipk
bin/packages/aarch64_cortex-a53/luci_app_xray/luci-app-xray-status_3.6.1-r1_all.ipk
```

**Note these paths** - you'll need them for the next step!

---

## Part 4: Transfer and Install on Router

### Step 9: Transfer IPK Files to Router

⚠️ **IMPORTANT:** Replace `YOUR_ARCHITECTURE` and `YOUR_ROUTER_IP` with your values!

#### Generic Command Template

Example: 
```bash
cd ~/openwrt-sdk
scp bin/packages/YOUR_ARCHITECTURE/luci_app_xray/luci-app-xray_*.ipk \
    bin/packages/YOUR_ARCHITECTURE/luci_app_xray/luci-app-xray-status_*.ipk \
    root@YOUR_ROUTER_IP:/tmp/
```

### Step 10: Install on Router

```bash
# Install all packages
opkg install xray-core_*.ipk xray-example_*.ipk luci-app-xray_*.ipk luci-app-xray-status_*.ipk
```

### Step 11: Access Xray in LuCI

1. Open your router's web interface (usually http://192.168.1.1)
2. Log in
3. Navigate to **Services** → **Xray**
4. Configure your Xray settings

---

## Part 5: Updating to Newer Versions

When `luci-app-xray` releases a new version, you have two options to rebuild.

### Option A: Quick Update (For Minor Updates)

```bash
cd ~/openwrt-sdk

# Update the feed to get latest changes
./scripts/feeds update luci_app_xray

# Reinstall the packages (forces update)
./scripts/feeds install -f luci-app-xray luci-app-xray-status

# Clean previous build artifacts
make package/feeds/luci_app_xray/{clean,download,prepare}

# Rebuild
make package/feeds/luci_app_xray/luci-app-xray/compile V=s -j$(nproc)

# Find new IPK files
find bin/ -name "*xray*.ipk"
```

### Option B: Complete Clean Rebuild (For Major Updates)

```bash
cd ~/openwrt-sdk

# Update feed
./scripts/feeds update luci_app_xray
./scripts/feeds install -f -a -p luci_app_xray

# Remove all previous xray build artifacts
rm -rf build_dir/target-*/luci-app-xray*
rm -rf build_dir/target-*/xray-*
rm -rf bin/packages/*/luci_app_xray/*
rm -rf bin/packages/*/packages/xray-*

# Rebuild everything from scratch
make package/feeds/luci_app_xray/luci-app-xray/compile V=s -j$(nproc)

# Find new packages
find bin/ -name "*xray*.ipk"
```

**When to use each option:**
- **Option A**: For version bumps (3.6.1 → 3.6.2)
- **Option B**: For major changes, dependency updates, or if Option A fails

---

## Alternative: Using GitHub Actions

If 2 hours of compilation is too long, consider using GitHub Actions to automate builds (TBH build time on GitHub is also close to 2 hours)

1. Fork the `luci-app-xray` repository
2. Enable GitHub Actions in your fork
3. The repository includes build workflows
4. Releases are automatically built and published

Check the repository's `.github/workflows/` directory for details.

---

## Additional Resources

- **Official Repository:** https://github.com/yichya/luci-app-xray
- **OpenWrt Downloads:** https://downloads.openwrt.org/releases/
- **Xray Documentation:** https://xtls.github.io/

## Next Steps: Configuring Xray

After successfully installing the packages, you'll need to configure Xray for your use case. See the complete configuration guide:

**[OpenWrt Xray (VLESS + REALITY) Complete Setup Guide](https://github.com/MarvinFS/public/blob/main/openwrt/openwrt_xray_vless_reality_how_to.md)**

This guide covers:
- Complete LuCI configuration (transparent proxy, DNS, routing)
- VLESS + REALITY server setup
- Geographic routing (Russia direct, blocked content via proxy)
- UDP transparent proxy for VoIP apps
- Troubleshooting common issues

---

Happy compiling!
