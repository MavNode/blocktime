import json
import subprocess
import time
import base64
import hashlib
from datetime import datetime
from collections import defaultdict, deque

POLL_INTERVAL = 0.4

FAST_THRESHOLD = 0.7
FAIL_THRESHOLD = 5.0

VALIDATORSET_REFRESH_EVERY = 200  # blocks
SAMPLE_MAX = 200

VALCONS_PREFIX = "shidovalcons"


# ----------------------------
# bech32 (BIP-0173) minimal impl
# ----------------------------
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_polymod(values):
    GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = (chk >> 25) & 0xFF
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk


def bech32_hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_create_checksum(hrp, data):
    values = bech32_hrp_expand(hrp) + data
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def bech32_encode(hrp, data):
    combined = data + bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join([CHARSET[d] for d in combined])


def convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def bytes20_to_valcons(addr20: bytes) -> str:
    data5 = convertbits(addr20, 8, 5, True)
    return bech32_encode(VALCONS_PREFIX, data5)


# ----------------------------
# chain queries
# ----------------------------
def run_cmd_json(cmd):
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    return json.loads(out)


def get_status():
    return run_cmd_json(["shidod", "status"])


def parse_tm_time(ts: str) -> datetime:
    if "." in ts:
        base, frac = ts.split(".")
        frac = frac[:6]
        ts = f"{base}.{frac}Z"
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def get_block(height: int):
    # Your CLI defaults to --type=hash; force height.
    return run_cmd_json(["shidod", "query", "block", "--type=height", str(height), "-o", "json"])


def extract_header(block_json: dict) -> dict:
    # Your chain returns {"header": {...}, "data": {...}, ...}
    return (
        block_json.get("header")
        or block_json.get("block", {}).get("header")
        or block_json.get("result", {}).get("block", {}).get("header")
        or {}
    )


def get_validator_set(height: int):
    return run_cmd_json(["shidod", "query", "tendermint-validator-set", str(height), "-o", "json"])


def get_staking_validators():
    return run_cmd_json(["shidod", "query", "staking", "validators", "-o", "json"])


def percentile_from_samples(samples, p=95):
    if not samples:
        return None
    s = sorted(samples)
    k = int(round((p / 100.0) * (len(s) - 1)))
    return s[max(0, min(k, len(s) - 1))]


# ----------------------------
# main
# ----------------------------
print("📡 Tendermint proposer diagnostics (Moniker + valcons)")
print("Press Ctrl+C to stop\n")

# Build pubkey(base64) -> moniker map from staking validators
pubkey_to_moniker = {}
try:
    sv = get_staking_validators()
    vals = sv.get("validators", [])
    for v in vals:
        desc = v.get("description", {}) or {}
        moniker = (desc.get("moniker") or "").strip()
        cpk = v.get("consensus_pubkey", {}) or {}
        pk_b64 = cpk.get("value")
        if pk_b64 and moniker:
            pubkey_to_moniker[pk_b64] = moniker
except Exception:
    # If this fails, we'll still run but show valcons only.
    pubkey_to_moniker = {}

# Cache: valcons -> {"pubkey_b64":..., "power":...}
valcons_map = {}
last_valset_height = None

# Stats
stats = defaultdict(lambda: {
    "count": 0,
    "sum": 0.0,
    "min": float("inf"),
    "max": 0.0,
    "fast": 0,
    "fail": 0,
    "samples": deque(maxlen=SAMPLE_MAX),
})

total_blocks = 0
total_time = 0.0
fast_blocks = 0
fail_blocks = 0

last_height = None
last_time = None


def refresh_valset(h: int):
    global valcons_map, last_valset_height
    vs = get_validator_set(h)
    vlist = vs.get("validators", []) or []
    newmap = {}
    for item in vlist:
        valcons = item.get("address")
        pubkey = (item.get("pub_key", {}) or {}).get("key")
        power = item.get("voting_power")
        if valcons and pubkey:
            newmap[valcons] = {"pubkey_b64": pubkey, "power": power}
    valcons_map = newmap
    last_valset_height = h


try:
    while True:
        status = get_status()
        height = int(status["sync_info"]["latest_block_height"])
        ts = parse_tm_time(status["sync_info"]["latest_block_time"])

        if last_height is None:
            last_height = height
            last_time = ts
            try:
                refresh_valset(height)
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)
            continue

        if height > last_height:
            blocks_advanced = height - last_height
            delta_total = (ts - last_time).total_seconds()

            if delta_total > 0:
                per_block = delta_total / blocks_advanced

                # Refresh validator set occasionally
                if last_valset_height is None or (height - last_valset_height >= VALIDATORSET_REFRESH_EVERY):
                    try:
                        refresh_valset(height)
                    except Exception:
                        pass

                for i in range(1, blocks_advanced + 1):
                    h = last_height + i

                    proposer_b64 = None
                    try:
                        blk = get_block(h)
                        header = extract_header(blk)
                        proposer_b64 = header.get("proposer_address")
                    except Exception:
                        proposer_b64 = None

                    valcons = "unknown"
                    if proposer_b64:
                        try:
                            addr20 = base64.b64decode(proposer_b64)
                            if len(addr20) == 20:
                                valcons = bytes20_to_valcons(addr20)
                        except Exception:
                            valcons = "unknown"

                    meta = valcons_map.get(valcons, {})
                    pubkey_b64 = meta.get("pubkey_b64")
                    moniker = pubkey_to_moniker.get(pubkey_b64, None) if pubkey_b64 else None

                    label = f"{moniker} ({valcons})" if moniker else valcons

                    # Update overall
                    total_blocks += 1
                    total_time += per_block
                    is_fast = per_block <= FAST_THRESHOLD
                    is_fail = per_block >= FAIL_THRESHOLD
                    if is_fast:
                        fast_blocks += 1
                    if is_fail:
                        fail_blocks += 1

                    # Update stats per validator (by valcons)
                    st = stats[valcons]
                    st["count"] += 1
                    st["sum"] += per_block
                    st["min"] = min(st["min"], per_block)
                    st["max"] = max(st["max"], per_block)
                    st["samples"].append(per_block)
                    if is_fast:
                        st["fast"] += 1
                    if is_fail:
                        st["fail"] += 1

                    print(
                        f"🧱 Height: {h} | "
                        f"⏱ {per_block:5.3f}s | "
                        f"👤 Proposer: {label} | "
                        f"{'⚡FAST' if is_fast else '     '} "
                        f"{'⛔FAIL' if is_fail else ''}",
                        flush=True
                    )

            last_height = height
            last_time = ts

        time.sleep(POLL_INTERVAL)

except KeyboardInterrupt:
    print("\n\n🛑 Monitor stopped\n")

    def pct(x, d):
        return (100.0 * x / d) if d else 0.0

    print("📊 Overall")
    print(f"Blocks observed      : {total_blocks}")
    print(f"Avg block time       : {(total_time / total_blocks):.3f}s" if total_blocks else "Avg block time       : n/a")
    print(f"FAST (<= {FAST_THRESHOLD:.1f}s) : {fast_blocks} ({pct(fast_blocks, total_blocks):.1f}%)")
    print(f"FAIL (>= {FAIL_THRESHOLD:.1f}s) : {fail_blocks} ({pct(fail_blocks, total_blocks):.1f}%)")

    rows = []
    for valcons, st in stats.items():
        c = st["count"]
        avg = st["sum"] / c if c else 0.0
        p95 = percentile_from_samples(list(st["samples"]), 95) or 0.0
        fast_r = pct(st["fast"], c)
        fail_r = pct(st["fail"], c)

        pubkey_b64 = valcons_map.get(valcons, {}).get("pubkey_b64")
        moniker = pubkey_to_moniker.get(pubkey_b64, "") if pubkey_b64 else ""
        label = f"{moniker} ({valcons})" if moniker else valcons

        rows.append((label, c, avg, p95, fast_r, fail_r, st["max"]))

    rows_5 = [r for r in rows if r[1] >= 5]

    rows_5.sort(key=lambda r: (r[5], r[2]), reverse=True)
    print("\n🏷️ Top offenders by FAIL-rate (min 5 blocks)")
    for label, c, avg, p95, fast_r, fail_r, mx in rows_5[:10]:
        print(f"- {label} | n={c} | fail={fail_r:.1f}% | fast={fast_r:.1f}% | avg={avg:.3f}s | p95~={p95:.3f}s | max={mx:.3f}s")

    rows_5.sort(key=lambda r: r[2], reverse=True)
    print("\n🐢 Top offenders by AVG block time (min 5 blocks)")
    for label, c, avg, p95, fast_r, fail_r, mx in rows_5[:10]:
        print(f"- {label} | n={c} | avg={avg:.3f}s | p95~={p95:.3f}s | fail={fail_r:.1f}% | max={mx:.3f}s")
