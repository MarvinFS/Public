# WireGuard Server on OpenWrt 24.10 (CLI Guide)

This guide configures a WireGuard **server** on OpenWrt 24.10 using **command-line only**.
It is adapted directly from the official OpenWrt WireGuard server documentation and aligned with fw4 (nftables).

Important note about firewall zones:
OpenWrt UCI firewall zones are often **anonymous sections** (firewall.@zone[0], firewall.@zone[1], …).
You must NOT assume a section named `firewall.lan` exists, even if the zone name is "lan".
This guide uses the correct anonymous section syntax.

---

## 1. Install Required Packages

```sh
opkg update
opkg install wireguard-tools kmod-wireguard luci-proto-wireguard
```

---

## 2. Define Configuration Parameters

```sh
VPN_IF="wg0"
VPN_PORT="51820"
VPN_ADDR="10.7.0.1/24"
VPN_ADDR6="fd00:7::1/64"
```

---

## 3. Prepare WireGuard Directory

```sh
mkdir -p /etc/wireguard
chmod 700 /etc/wireguard
```

---

## 4. Key Management

```sh
umask 077

wg genkey | tee /etc/wireguard/server.key | wg pubkey > /etc/wireguard/server.pub
wg genkey | tee /etc/wireguard/client1.key | wg pubkey > /etc/wireguard/client1.pub
wg genpsk > /etc/wireguard/client1.psk

VPN_KEY="$(cat /etc/wireguard/server.key)"
VPN_PUB="$(cat /etc/wireguard/client1.pub)"
VPN_PSK="$(cat /etc/wireguard/client1.psk)"
```

---

## 5. Network Configuration + add first config which is mandatory - change IPs

```sh
uci -q delete network.${VPN_IF}
uci set network.${VPN_IF}="interface"
uci set network.${VPN_IF}.proto="wireguard"
uci set network.${VPN_IF}.private_key="${VPN_KEY}"
uci set network.${VPN_IF}.listen_port="${VPN_PORT}"
uci add_list network.${VPN_IF}.addresses="${VPN_ADDR}"
uci add_list network.${VPN_IF}.addresses="${VPN_ADDR6}"
```

```sh
uci -q delete network.wgclient1
uci set network.wgclient1="wireguard_${VPN_IF}"
uci set network.wgclient1.public_key="${VPN_PUB}"
uci set network.wgclient1.preshared_key="${VPN_PSK}"
uci add_list network.wgclient1.allowed_ips="10.7.0.2/32"
uci add_list network.wgclient1.allowed_ips="fd00:7::2/128"
```

```sh
uci commit network
service network restart
```

---

## 6. Firewall Configuration

### 6.1 Identify LAN Zone Section

```sh
uci show firewall | grep -E "=zone"
uci show firewall | grep -E "\.name='lan'$"
```

Expected example result:
```
firewall.@zone[0].name='lan'
```

---

### 6.2 Attach WireGuard Interface to LAN Zone

```sh
uci del_list firewall.@zone[0].network="${VPN_IF}"
uci add_list firewall.@zone[0].network="${VPN_IF}"
```

---

### 6.3 Allow WireGuard From WAN

```sh
uci -q delete firewall.wireguard_in
uci set firewall.wireguard_in="rule"
uci set firewall.wireguard_in.name="Allow-WireGuard"
uci set firewall.wireguard_in.src="wan"
uci set firewall.wireguard_in.proto="udp"
uci set firewall.wireguard_in.dest_port="${VPN_PORT}"
uci set firewall.wireguard_in.target="ACCEPT"
```

---

### 6.4 Apply Firewall Configuration

```sh
uci commit firewall
fw4 reload
```

---

## 7. Client Configuration
## for split VPN replace applowedIPs 
## AllowedIPs = 192.168.0.0/16, 10.7.0.0/24

```ini
[Interface]
PrivateKey = <client1.key>
Address = 10.7.0.2/32, fd00:7::2/128
DNS = 192.168.1.1

[Peer]
PublicKey = <server.pub>
PresharedKey = <client1.psk>
Endpoint = <router-public-ip-or-ddns>:51820
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
```

---

## 8. Verification

```sh
wg show
ip address show ${VPN_IF}
uci show firewall.@zone[0].network
nft list ruleset | grep 51820
```

---

## 9. Testing

```sh
traceroute openwrt.org
traceroute6 openwrt.org
```

---

## 10. Troubleshooting

```sh
service log restart
service network restart
sleep 10

logread | grep -i wireguard
wg show
wg showconf ${VPN_IF}
ip address show
ip route show table all
ip rule show
ip -6 rule show
nft list ruleset
uci show network
uci show firewall
```
