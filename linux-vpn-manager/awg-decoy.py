#!/usr/bin/env python3
"""Print an AmneziaWG 1.5 decoy (I1) that looks like a real UDP DNS query.

AmneziaWG clients may send up to five unencrypted packets (I1-I5) before the
WireGuard handshake so the start of a flow resembles a protocol a filter lets
through. The value is client-side only: a stock WireGuard server ignores it.

Why DNS first: Russia's TSPU decrypts the first packet of UDP flows and drops
QUIC v1 whose SNI is on the blocklist (measured on all ports in 2026), and most
Russian public services do not serve HTTP/3, so a QUIC decoy naming one of them
is not plausible. A small DNS query cannot trip that filter. Field reports from
2026 also show a QUIC decoy with an allowed SNI helping on some ISPs, and a
short realistic first packet passing where 1200 random bytes did not. There is
no universal recipe, so treat the decoy as something to test per ISP.

Port: the vendor wiki advises UDP/53 for DNS mimicry (forward WAN UDP/53 to the
WireGuard port and use it in the client Endpoint), while one 2026 Moscow test
got a mimicry decoy through on an ordinary high port. Try both.

Every client should get its own value (a decoy shared by all clients is a
fingerprint), so this picks a random domain and query style on each run. The
two transaction-id bytes are a CPS <r 2> tag, so they change on every send.

Standard library only. Usage:
    awg-decoy.py                    one random decoy
    awg-decoy.py --domain ya.ru     fixed domain
    awg-decoy.py --list             show the domain pool
    awg-decoy.py --selftest         decode what it builds and check it
"""
import argparse
import random
import re
import struct
import sys

# Public Russian services with no reason to sit on a blocklist.
POOL = [
    "ya.ru", "yandex.ru", "dzen.ru", "vk.com", "ok.ru", "mail.ru",
    "gosuslugi.ru", "sberbank.ru", "avito.ru", "ozon.ru", "wildberries.ru",
    "rbc.ru", "kinopoisk.ru", "hh.ru", "2gis.ru", "mos.ru",
]
QTYPES = {"A": 1, "AAAA": 28}


def qname(domain):
    out = b""
    for label in domain.strip(".").split("."):
        raw = label.encode("ascii")
        if not 0 < len(raw) < 64:
            raise ValueError("bad DNS label: %r" % label)
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def build_query(domain, qtype="A", edns=False):
    """DNS message without the 2-byte transaction id (flags onward)."""
    msg = struct.pack(">HHHHH", 0x0100, 1, 0, 0, 1 if edns else 0)
    msg += qname(domain) + struct.pack(">HH", QTYPES[qtype], 1)
    if edns:
        # OPT pseudo-record: root name, TYPE 41, UDP payload size, TTL 0, no options
        msg += b"\x00" + struct.pack(">HHIH", 41, 1232, 0, 0)
    return msg


def cps(domain, qtype="A", edns=False):
    return "<r 2><b 0x%s>" % build_query(domain, qtype, edns).hex()


def parse_cps(value):
    """Rebuild the datagram from our own CPS string (transaction id = 0)."""
    m = re.fullmatch(r"<r 2><b 0x([0-9a-f]+)>", value)
    if not m:
        raise ValueError("unexpected CPS shape")
    return b"\x00\x00" + bytes.fromhex(m.group(1))


def decode(datagram):
    """Tiny DNS query decoder, independent of the builder."""
    txid, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", datagram[:12])
    pos, labels = 12, []
    while datagram[pos]:
        n = datagram[pos]
        labels.append(datagram[pos + 1:pos + 1 + n].decode("ascii"))
        pos += 1 + n
    pos += 1
    qtype, qclass = struct.unpack(">HH", datagram[pos:pos + 4])
    pos += 4
    return {"txid": txid, "flags": flags, "qd": qd, "an": an, "ns": ns, "ar": ar,
            "name": ".".join(labels), "qtype": qtype, "qclass": qclass, "tail": datagram[pos:]}


def selftest():
    for domain in POOL:
        for qt in QTYPES:
            for edns in (False, True):
                d = decode(parse_cps(cps(domain, qt, edns)))
                assert d["name"] == domain and d["qtype"] == QTYPES[qt] and d["qclass"] == 1, d
                assert d["flags"] == 0x0100 and d["qd"] == 1 and d["an"] == d["ns"] == 0, d
                assert d["ar"] == (1 if edns else 0), d
                assert d["tail"] == (b"\x00\x00\x29\x04\xd0\x00\x00\x00\x00\x00\x00" if edns else b""), d
    print("selftest ok: %d domains x %d qtypes x 2 styles" % (len(POOL), len(QTYPES)))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--domain", help="fixed domain instead of a random pool entry")
    p.add_argument("--qtype", choices=sorted(QTYPES), help="default: random A (80%%) or AAAA")
    p.add_argument("--style", choices=("plain", "edns"), help="default: random")
    p.add_argument("--seed", type=int, help="make the random choices repeatable")
    p.add_argument("--list", action="store_true", help="print the domain pool")
    p.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.list:
        print("\n".join(POOL))
        return 0
    if a.selftest:
        selftest()
        return 0
    rng = random.Random(a.seed) if a.seed is not None else random.SystemRandom()
    domain = a.domain or rng.choice(POOL)
    qtype = a.qtype or ("A" if rng.random() < 0.8 else "AAAA")
    edns = (a.style == "edns") if a.style else rng.random() < 0.5
    print(cps(domain, qtype, edns))
    return 0


if __name__ == "__main__":
    sys.exit(main())
