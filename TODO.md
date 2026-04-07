# TODO: Nuanic Multi-Ring Stability Refinement

## High Priority: Warmup & Connection "Double-Try" Investigation
Currently, even with the automated **Universal Warmup** protocol, we often see a `Device not found` error on the reconnection phase or require a second manual restart of the script to stabilize the stream. 

### Core Hypothesis
- **BT Stack Latency**: The Windows Bluetooth stack (WinRT) is not purging the ACL link fast enough after the `[WARMUP]` disconnect. When the script tries to reconnect 2 seconds later, the radio reports the device is "not found" because it thinks it's still technically in a cleanup state.
- **Race Condition**: The staggered connection logic for multi-ring sessions might need longer inter-device delays (currently 0.5s–3.0s) when the recursive `_connect_and_subscribe` is firing warmup cycles.

### Action Items for Tomorrow
- [ ] **Adaptive Delay**: Increase the post-warmup `asyncio.sleep(2.0)` to a configurable value or an adaptive 3-5s delay.
- [ ] **Internal Retry Persistence**: Implement a more aggressive internal retry loop *specifically* for the post-warmup reconnection (currently it uses standard 3-attempt logic).
- [ ] **Radio Flush Utility**: Investigate if a programmatic "BT Cache Flush" can be triggered via `Bleak` before the final connection.
- [ ] **16 Hz Physical Validation**: Confirm if the 16 Hz ceiling is consistent across different Nuanic hardware versions.

---
*Created: 2026-04-07*
