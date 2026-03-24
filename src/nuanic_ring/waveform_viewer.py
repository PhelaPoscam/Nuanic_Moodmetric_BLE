"""Standalone live viewer for Juho-like Nuanic streams.

Plots (UUID-anchored, candidate semantics):
- 42dcb71b LIVE_EDA signal metric (HQI ohm or fallback int16 mean-abs)
- 42dcb71b LIVE_EDA ohm (when HQI 14-byte packets are available)
- d306262b LIVE_DNA word0..word3 (uint32 little-endian)

Notes:
- word0 is treated as a clock/timestamp candidate and shown as diagnostics text (not plotted as waveform).
- word1 is often constant 0 in current captures (context/session candidate).
- word3 appears closer to a DNE/MM-like index candidate than a generic quality score.
"""

import asyncio
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone

import matplotlib.pyplot as plt
import numpy as np

from .connector import NuanicConnector


def smooth_data(data: list, window: int) -> list:
    """Apply moving-average smoothing."""
    if not data or window <= 1:
        return data
    if len(data) < window:
        return data

    kernel = np.ones(window) / window
    smoothed = np.convolve(data, kernel, mode="valid")
    pad_length = len(data) - len(smoothed)
    padded = np.pad(smoothed, (pad_length, 0), mode="edge")
    return padded.tolist()


class WaveformState:
    """Thread-safe container for live buffers used by the plotter."""

    def __init__(self, history_points: int = 600):
        self.lock = threading.Lock()

        self.live_eda_packets = 0
        self.live_dna_packets = 0
        self.latest_eda_mode = "none"

        self.live_eda_signal_index = deque(maxlen=history_points)
        self.live_eda_signal_wave = deque(maxlen=history_points)
        self.live_eda_ohm_index = deque(maxlen=history_points)
        self.live_eda_ohm_wave = deque(maxlen=history_points)

        self.live_dna_index = deque(maxlen=history_points)
        self.live_dna_pc_seconds = deque(maxlen=history_points)
        self.live_dna_word0 = deque(maxlen=history_points)
        self.live_dna_word1 = deque(maxlen=history_points)
        self.live_dna_word2 = deque(maxlen=history_points)
        self.live_dna_word3 = deque(maxlen=history_points)


class NuanicWaveformViewer:
    """Connects to ring and exposes LIVE_EDA/LIVE_DNA data for plotting."""

    def __init__(self, ring_addr: str | None = None):
        self.connector = NuanicConnector(target_address=ring_addr)
        self.state = WaveformState()
        self._running = False

    def _live_eda_callback(self, sender, data):
        with self.state.lock:
            self.state.live_eda_packets += 1
            packet_id = self.state.live_eda_packets

            # Juho decode path: 14-byte <HQI packet
            if len(data) == 14:
                _boot_count, _timestamp_ms, eda_ohm = struct.unpack("<HQI", bytes(data))
                signal_value = float(eda_ohm)
                self.state.latest_eda_mode = "HQI"

                self.state.live_eda_ohm_index.append(packet_id)
                self.state.live_eda_ohm_wave.append(float(eda_ohm))

                self.state.live_eda_signal_index.append(packet_id)
                self.state.live_eda_signal_wave.append(signal_value)
                return

            # Fallback decode: int16 stream -> mean(abs(sample))
            if len(data) >= 2 and len(data) % 2 == 0:
                sample_count = len(data) // 2
                samples = struct.unpack("<" + ("h" * sample_count), bytes(data))
                mean_abs = sum(abs(int(v)) for v in samples) / max(1, sample_count)

                self.state.latest_eda_mode = "int16"
                self.state.live_eda_signal_index.append(packet_id)
                self.state.live_eda_signal_wave.append(float(mean_abs))
                return

            # Unknown payload shape, keep packet count only.
            self.state.latest_eda_mode = "raw"

    def _live_dna_callback(self, sender, data):
        if len(data) != 16:
            return

        word0, word1, word2, word3 = struct.unpack("<IIII", bytes(data))

        with self.state.lock:
            self.state.live_dna_packets += 1
            packet_id = self.state.live_dna_packets
            self.state.live_dna_index.append(packet_id)
            self.state.live_dna_pc_seconds.append(time.perf_counter())
            self.state.live_dna_word0.append(float(word0))
            self.state.live_dna_word1.append(float(word1))
            self.state.live_dna_word2.append(float(word2))
            self.state.live_dna_word3.append(float(word3))


def _epoch_candidate_text(raw_value: int, current_utc: datetime) -> str:
    """Heuristic check whether raw_value could be unix sec/ms/us timestamp."""
    candidates: list[str] = []
    specs = [
        ("sec", 1.0),
        ("ms", 1_000.0),
        ("us", 1_000_000.0),
    ]

    for label, divisor in specs:
        try:
            dt = datetime.fromtimestamp(raw_value / divisor, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue

        delta_seconds = abs((current_utc - dt).total_seconds())
        # "plausible-now" means within 24h of host UTC time.
        plausible_now = delta_seconds <= 86400
        if plausible_now:
            candidates.append(f"{label}=YES ({dt.isoformat()})")
        else:
            candidates.append(f"{label}=no ({dt.date().isoformat()})")

    if not candidates:
        return "sec/ms/us: out-of-range"
    return " | ".join(candidates)


# Methods for NuanicWaveformViewer are added below via assignment


async def _connect_and_subscribe(self) -> bool:
    if not await self.connector.connect():
        return False

    live_dna_ok = await self.connector.subscribe_to_imu(self._live_dna_callback)
    live_eda_ok = await self.connector.subscribe_to_live_eda(self._live_eda_callback)

    if not (live_dna_ok and live_eda_ok):
        await self.connector.unsubscribe_from_imu()
        await self.connector.unsubscribe_from_live_eda()
        await self.connector.disconnect()
        return False

    self._running = True
    return True


async def _run_until_stopped(self):
    try:
        while self._running:
            await asyncio.sleep(0.1)
    finally:
        await self.stop()


async def _stop(self):
    self._running = False
    await self.connector.unsubscribe_from_imu()
    await self.connector.unsubscribe_from_live_eda()
    await self.connector.disconnect()


# Assign methods to NuanicWaveformViewer class
NuanicWaveformViewer.connect_and_subscribe = _connect_and_subscribe
NuanicWaveformViewer.run_until_stopped = _run_until_stopped
NuanicWaveformViewer.stop = _stop


def _autoscale_axis(
    axis, line, x_data: list[float], y_data: list[float], smooth_window: int
):
    if not x_data or not y_data:
        return

    y_smooth = smooth_data(y_data, smooth_window)
    line.set_data(x_data, y_smooth)
    axis.set_xlim(min(x_data), max(x_data))

    ymin = min(y_smooth)
    ymax = max(y_smooth)
    if ymin == ymax:
        pad = max(1.0, abs(float(ymin)) * 0.02)
    else:
        pad = max(1.0, (ymax - ymin) * 0.1)
    axis.set_ylim(ymin - pad, ymax + pad)


async def run_plot_async(
    viewer: NuanicWaveformViewer,
    window_seconds: int,
    refresh_ms: int,
    smooth_window: int = 1,
):
    """Run matplotlib UI while asyncio keeps BLE callbacks flowing."""
    plt.ioff()  # Turn off interactive mode to avoid event loop conflicts

    fig, axes = plt.subplots(3, 2, figsize=(13, 9), sharex=False)
    fig.suptitle("Nuanic Ring: 42dc LIVE_EDA + d306 LIVE_DNA (candidate semantics)")

    ax_eda_signal = axes[0][0]
    ax_eda_ohm = axes[0][1]
    ax_w0 = axes[1][0]
    ax_w1 = axes[1][1]
    ax_w2 = axes[2][0]
    ax_w3 = axes[2][1]

    (line_eda_signal,) = ax_eda_signal.plot([], [], lw=1.4, color="tab:blue")
    (line_eda_ohm,) = ax_eda_ohm.plot([], [], lw=1.4, color="tab:green")
    (line_w1,) = ax_w1.plot([], [], lw=1.2, color="tab:red")
    (line_w2,) = ax_w2.plot([], [], lw=1.2, color="tab:purple")
    (line_w3,) = ax_w3.plot([], [], lw=1.2, color="tab:brown")

    ax_eda_signal.set_title("42dcb71b LIVE_EDA signal (HQI ohm or int16 mean-abs)")
    ax_eda_ohm.set_title("42dcb71b LIVE_EDA ohm (HQI 14-byte only)")
    ax_w0.set_title("d306262b word0 timestamp/clock diagnostics")
    ax_w1.set_title("d306262b word1 (session/context candidate)")
    ax_w2.set_title("d306262b word2 (signal candidate)")
    ax_w3.set_title("d306262b word3 (DNE/MM-like index candidate)")

    for axis in [ax_eda_signal, ax_eda_ohm, ax_w1, ax_w2, ax_w3]:
        axis.set_xlabel("Packet index")
        axis.set_ylabel("Value")

    ax_w0.axis("off")
    word0_text = ax_w0.text(
        0.01,
        0.98,
        "waiting for d306 packets...",
        transform=ax_w0.transAxes,
        va="top",
        ha="left",
        fontsize=10,
        family="monospace",
    )

    status_text = fig.text(0.01, 0.975, "Waiting for packets...", fontsize=10)
    max_points = max(200, window_seconds * 100)

    plt.tight_layout()
    # Show the figure window
    fig.show()
    fig.canvas.draw()

    try:
        while viewer._running:
            # Check if figure window was closed by user
            if not plt.fignum_exists(fig.number):
                break
            with viewer.state.lock:
                eda_signal_x = list(viewer.state.live_eda_signal_index)[-max_points:]
                eda_signal_y = list(viewer.state.live_eda_signal_wave)[-max_points:]

                eda_ohm_x = list(viewer.state.live_eda_ohm_index)[-max_points:]
                eda_ohm_y = list(viewer.state.live_eda_ohm_wave)[-max_points:]

                dna_x = list(viewer.state.live_dna_index)[-max_points:]
                dna_t = list(viewer.state.live_dna_pc_seconds)[-max_points:]
                w0 = list(viewer.state.live_dna_word0)[-max_points:]
                w1 = list(viewer.state.live_dna_word1)[-max_points:]
                w2 = list(viewer.state.live_dna_word2)[-max_points:]
                w3 = list(viewer.state.live_dna_word3)[-max_points:]

                live_eda_packets = viewer.state.live_eda_packets
                live_dna_packets = viewer.state.live_dna_packets
                latest_eda_mode = viewer.state.latest_eda_mode

            _autoscale_axis(
                ax_eda_signal,
                line_eda_signal,
                eda_signal_x,
                eda_signal_y,
                smooth_window,
            )
            _autoscale_axis(
                ax_eda_ohm, line_eda_ohm, eda_ohm_x, eda_ohm_y, smooth_window
            )
            _autoscale_axis(ax_w1, line_w1, dna_x, w1, smooth_window)
            _autoscale_axis(ax_w2, line_w2, dna_x, w2, smooth_window)
            _autoscale_axis(ax_w3, line_w3, dna_x, w3, smooth_window)

            latest_w0 = int(w0[-1]) if w0 else None
            first_w0 = int(w0[0]) if w0 else None
            prev_w0 = int(w0[-2]) if len(w0) >= 2 else None
            step_w0 = (
                (latest_w0 - prev_w0)
                if latest_w0 is not None and prev_w0 is not None
                else None
            )
            rel_w0 = (
                (latest_w0 - first_w0)
                if latest_w0 is not None and first_w0 is not None
                else None
            )

            latest_w1 = int(w1[-1]) if w1 else None
            latest_w2 = int(w2[-1]) if w2 else None
            latest_w3 = int(w3[-1]) if w3 else None
            w1_zero_ratio = (
                (sum(1 for value in w1 if int(value) == 0) / len(w1)) if w1 else None
            )
            w1_ratio_text = (
                f"{(w1_zero_ratio * 100):.1f}%" if w1_zero_ratio is not None else "n/a"
            )
            latest_w2_text = (
                f"{latest_w2} (0x{latest_w2:08X})" if latest_w2 is not None else "n/a"
            )
            latest_w3_text = (
                f"{latest_w3} (0x{latest_w3:08X})" if latest_w3 is not None else "n/a"
            )

            clock_mode = "n/a"
            monotonic_ratio_text = "n/a"
            if len(w0) >= 3:
                monotonic_steps = sum(
                    1 for i in range(1, len(w0)) if int(w0[i]) >= int(w0[i - 1])
                )
                total_steps = len(w0) - 1
                monotonic_ratio = monotonic_steps / max(1, total_steps)
                monotonic_ratio_text = f"{(monotonic_ratio * 100):.1f}%"
                monotonic = monotonic_ratio >= 0.99
                clock_mode = (
                    "monotonic counter/clock"
                    if monotonic
                    else "non-monotonic (not pure counter)"
                )

            tick_rate_text = "n/a"
            if len(w0) >= 5 and len(dna_t) == len(w0):
                t0 = dna_t[0]
                x = np.array([t - t0 for t in dna_t], dtype=float)
                y = np.array(w0, dtype=float)
                if np.ptp(x) > 0:
                    slope, _intercept = np.polyfit(x, y, 1)
                    tick_rate_text = f"{slope:.3f} ticks/s"

            epoch_hint_text = "n/a"
            if latest_w0 is not None:
                epoch_hint_text = _epoch_candidate_text(
                    latest_w0,
                    datetime.now(timezone.utc),
                )

            latest_w0_text = (
                f"{latest_w0} (0x{latest_w0:08X})" if latest_w0 is not None else "n/a"
            )
            step_w0_text = str(step_w0) if step_w0 is not None else "n/a"
            rel_w0_text = str(rel_w0) if rel_w0 is not None else "n/a"

            word0_text.set_text(
                "word0 diagnostics\n"
                f"latest: {latest_w0_text}\n"
                f"step (latest-prev): {step_w0_text}\n"
                f"relative (latest-first): {rel_w0_text}\n"
                f"mode hint: {clock_mode}\n"
                f"monotonicity: {monotonic_ratio_text}\n"
                f"estimated tick rate: {tick_rate_text}\n"
                f"epoch plausibility: {epoch_hint_text}\n"
                "note: if sec/ms/us all say 'no', this is likely device-local time"
            )

            status_text.set_text(
                f"LIVE_EDA packets: {live_eda_packets} (mode: {latest_eda_mode}) | "
                f"LIVE_DNA packets: {live_dna_packets} | "
                f"w0 mode: {clock_mode} | "
                f"w1 latest: {latest_w1}, zero-rate: {w1_ratio_text} | "
                f"w2 latest: {latest_w2_text} | "
                f"w3 latest: {latest_w3_text}"
            )

            fig.canvas.draw_idle()
            fig.canvas.flush_events()
            # Use async sleep without matplotlib event processing
            await asyncio.sleep(refresh_ms / 1000.0)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            plt.close(fig)
        except Exception:
            pass


async def run_waveform_viewer(
    ring_addr: str | None = None,
    window_seconds: int = 10,
    refresh_ms: int = 120,
    smooth_window: int = 1,
) -> int:
    """Run standalone Juho-like live plotter."""
    viewer = NuanicWaveformViewer(ring_addr=ring_addr)

    if not await viewer.connect_and_subscribe():
        print("[FAIL] Could not connect and subscribe to LIVE_EDA/LIVE_DNA streams")
        return 1

    print("[OK] Connected. Opening Juho-like live plot window...")
    if smooth_window > 1:
        print(f"[SMOOTH] Applying {smooth_window}-point moving average filter")

    worker = asyncio.create_task(viewer.run_until_stopped())

    try:
        await run_plot_async(
            viewer,
            window_seconds=window_seconds,
            refresh_ms=refresh_ms,
            smooth_window=smooth_window,
        )
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user")
    finally:
        viewer._running = False
        await viewer.stop()
        try:
            await asyncio.wait_for(worker, timeout=2.0)
        except asyncio.TimeoutError:
            worker.cancel()

    print("[STOP] Waveform viewer stopped")
    return 0
