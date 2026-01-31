import json
import subprocess
import time
from datetime import datetime
from collections import deque

WINDOW = 50
POLL_INTERVAL = 0.4


def get_status():
    out = subprocess.check_output(
        ["shidod", "status"],
        stderr=subprocess.DEVNULL
    )
    return json.loads(out)


def parse_tm_time(ts: str) -> datetime:
    # Tendermint timestamps can be like: 2025-01-01T00:00:00.123456789Z
    # Python only supports up to microseconds (6 digits).
    if "." in ts:
        base, frac = ts.split(".")
        frac = frac[:6]  # keep microseconds
        ts = f"{base}.{frac}Z"
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


print("📡 Shido real-time block monitor")
print("Press Ctrl+C to stop\n")

last_height = None
last_time = None

# Sliding window storage
block_times = deque()
window_sum = 0.0

# Global statistics
total_blocks = 0
total_time = 0.0
min_bt = float("inf")   # fastest (smallest)
max_bt = 0.0            # slowest (largest)

try:
    while True:
        status = get_status()
        height = int(status["sync_info"]["latest_block_height"])
        ts = parse_tm_time(status["sync_info"]["latest_block_time"])

        if last_height is None:
            last_height = height
            last_time = ts
            time.sleep(POLL_INTERVAL)
            continue

        if height > last_height:
            blocks_advanced = height - last_height
            delta_total = (ts - last_time).total_seconds()

            if delta_total > 0:
                per_block = delta_total / blocks_advanced

                # Print one line per block height advanced
                for i in range(1, blocks_advanced + 1):
                    current_height = last_height + i

                    block_times.append(per_block)
                    window_sum += per_block

                    if len(block_times) > WINDOW:
                        window_sum -= block_times.popleft()

                    total_blocks += 1
                    total_time += per_block
                    min_bt = min(min_bt, per_block)
                    max_bt = max(max_bt, per_block)

                    avg = window_sum / len(block_times)
                    bps = (1 / avg) if avg > 0 else 0

                    print(
                        f"🧱 Height: {current_height} | "
                        f"⏱ Last: {per_block:5.3f}s | "
                        f"📊 Avg({len(block_times)}): {avg:5.3f}s | "
                        f"⚡ {bps:4.2f} blk/s"
                    )

            last_height = height
            last_time = ts

        time.sleep(POLL_INTERVAL)

except KeyboardInterrupt:
    print("\n🛑 Monitor stopped\n")

    if total_blocks > 0:
        final_avg = total_time / total_blocks
        print("📊 Final statistics")
        print(f"🧱 Blocks observed : {total_blocks}")
        print(f"⏱  Total time     : {total_time:.2f}s")
        print(f"⚡ Avg block time : {final_avg:.3f}s")
        print(f"🚀 Min block time : {min_bt:.3f}s  (fastest)")
        print(f"🐢 Max block time : {max_bt:.3f}s  (slowest)")
    else:
        print("⚠️  No blocks observed")
