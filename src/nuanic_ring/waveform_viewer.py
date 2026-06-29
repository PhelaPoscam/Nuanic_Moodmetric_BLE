"""Standalone live viewer for Juho-like Nuanic streams.

Plots (UUID-anchored, candidate semantics):

ponytail: matplotlib runs in a thread pool (run_in_executor) so the GUI
event loop never shares a thread with asyncio. Shared state guarded by
threading.Lock.
- 42dcb71b LIVE_EDA signal metric (HQI ohm or fallback int16 mean-abs)
- 42dcb71b LIVE_EDA ohm (when HQI 14-byte packets are available)
- d306262b LIVE_DNA word0..word3 (uint32 little-endian)

Notes:
- word0 is treated as a clock/timestamp candidate and shown as diagnostics text (not plotted as waveform).
- word1 is often constant 0 in current captures (context/session candidate).
- word3 appears closer to a DNE/MM-like index candidate than a generic quality score.
"""

import asyncio
import math
import struct
import threading
import time
from collections import deque

import matplotlib.pyplot as plt
import numpy as np

from .connector import NuanicConnector
from .mm_compat import MMFeatures, MMLikeScorer, convert_eda
from .signal_processing import SignalConditioner


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

        self.live_eda_signal_index: deque[float] = deque(maxlen=history_points)
        self.live_eda_signal_wave: deque[float] = deque(maxlen=history_points)
        self.live_eda_ohm_index: deque[float] = deque(maxlen=history_points)
        self.live_eda_ohm_wave: deque[float] = deque(maxlen=history_points)

        self.live_dna_index: deque[float] = deque(maxlen=history_points)
        self.live_dna_pc_seconds: deque[float] = deque(maxlen=history_points)
        self.live_dna_word0: deque[float] = deque(maxlen=history_points)
        self.live_dna_word1: deque[float] = deque(maxlen=history_points)
        self.live_dna_word2: deque[float] = deque(maxlen=history_points)
        self.live_dna_word3: deque[float] = deque(maxlen=history_points)

        self.imu_packets = 0
        self.imu_index: deque[float] = deque(maxlen=history_points)
        self.imu_intensity: deque[float] = deque(maxlen=history_points)

        self.mm_arousal_wave: deque[float] = deque(maxlen=history_points)
        self.mm_filtered_us_wave: deque[float] = deque(maxlen=history_points)
        self.mm_calibration_remaining = 0.0
        self.mm_calibrated = False


class NuanicWaveformViewer:
    """Connects to ring and exposes LIVE_EDA/LIVE_DNA data for plotting."""

    def __init__(
        self,
        ring_addr: str | None = None,
        calibration_seconds: int = 60,
        target_hz: float | None = None,
        attempt_rate_control: bool = False,
        raw_signal: bool = False,
    ):
        self.connector = NuanicConnector(target_address=ring_addr)
        self.state = WaveformState()
        self.signal_conditioner = SignalConditioner()
        self.raw_signal = raw_signal
        self.scorer = MMLikeScorer(calibration_seconds=calibration_seconds)
        self.target_hz = target_hz
        self.attempt_rate_control = attempt_rate_control
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
        _res, conductance_us = convert_eda(word2)

        with self.state.lock:
            self.state.live_dna_packets += 1
            packet_id = self.state.live_dna_packets
            self.state.live_dna_index.append(packet_id)
            self.state.live_dna_pc_seconds.append(time.perf_counter())
            self.state.live_dna_word0.append(float(word0))
            self.state.live_dna_word1.append(float(word1))
            self.state.live_dna_word2.append(float(word2))
            self.state.live_dna_word3.append(float(word3))

            # Process through Physiological Pipeline
            filtered_us = (
                conductance_us
                if self.raw_signal
                else self.signal_conditioner.process(conductance_us)
            )
            freq, amp = self.scorer.update_scr_features(tonic_value=filtered_us)
            features = MMFeatures(
                scr_frequency_per_min=freq,
                scr_amplitude=amp,
                scl_microsiemens=filtered_us,
            )
            score_state = self.scorer.update(features)

            self.state.mm_arousal_wave.append(score_state["mm_like_1_to_100"])
            self.state.mm_filtered_us_wave.append(filtered_us)
            self.state.mm_calibration_remaining = score_state[
                "calibration_seconds_remaining"
            ]
            self.state.mm_calibrated = score_state["calibrated"]

    def _imu_callback(self, sender, data):
        if len(data) != 92:
            return

        offset = 8
        mags = []
        for _ in range(14):
            x, y, z = struct.unpack_from("<hhh", bytes(data), offset)
            mags.append(math.sqrt((x * x) + (y * y) + (z * z)))
            offset += 6

        intensity = sum(mags) / max(1, len(mags))

        with self.state.lock:
            self.state.imu_packets += 1
            self.state.imu_index.append(self.state.imu_packets)
            self.state.imu_intensity.append(intensity)

    async def connect_and_subscribe(self) -> bool:
        if not await self.connector.connect():
            return False

        if self.attempt_rate_control and self.target_hz:
            print(f"[RATE] Requesting {self.target_hz} Hz sample rate...")
            await self.connector.attempt_set_sample_rate(target_hz=int(self.target_hz))

        live_dna_ok = await self.connector.subscribe_to_stress(self._live_dna_callback)
        imu_ok = await self.connector.subscribe_to_imu(self._imu_callback)
        live_eda_ok = await self.connector.subscribe_to_live_eda(
            self._live_eda_callback
        )

        if not (live_dna_ok and imu_ok and live_eda_ok):
            print("[WARN] Some telemetry streams failed to subscribe")
            if not live_dna_ok:
                await self.connector.unsubscribe_from_stress()
                await self.connector.unsubscribe_from_imu()
                await self.connector.unsubscribe_from_live_eda()
                await self.connector.disconnect()
                return False

        self._running = True
        return True

    async def run_until_stopped(self):
        try:
            while self._running:
                await asyncio.sleep(0.1)
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        await self.connector.unsubscribe_from_stress()
        await self.connector.unsubscribe_from_imu()
        await self.connector.unsubscribe_from_live_eda()
        await self.connector.disconnect()


def _autoscale_axis(
    axis, line, x_data: list[float], y_data: list[float], smooth_window: int
):
    if not x_data or not y_data:
        return

    y_smooth = smooth_data(y_data, smooth_window)
    line.set_data(x_data, y_smooth)
    x_min, x_max = min(x_data), max(x_data)
    if x_min == x_max:
        axis.set_xlim(x_min - 1, x_max + 1)
    else:
        axis.set_xlim(x_min, x_max)

    ymin = min(y_smooth)
    ymax = max(y_smooth)
    if ymin == ymax:
        # Use a very small pad if we're dealing with uS (usually < 10)
        # but a larger one for scores or integers.
        if abs(ymin) < 100:
            pad = max(0.001, abs(float(ymin)) * 0.01)
        else:
            pad = max(1.0, abs(float(ymin)) * 0.01)
    else:
        pad = max(0.0001, (ymax - ymin) * 0.1)
    axis.set_ylim(ymin - pad, ymax + pad)


def _run_plot_blocking(
    viewer: NuanicWaveformViewer,
    window_seconds: int,
    refresh_ms: int,
    smooth_window: int = 1,
):
    """Run matplotlib UI in a dedicated thread.

    By keeping matplotlib off the asyncio event loop we avoid the
    heisenbugs that come from mixing GUI event loops with async I/O.
    Shared state is protected by ``viewer.state.lock``.
    """
    plt.style.use("dark_background")
    plt.ioff()

    fig, axes = plt.subplots(3, 2, figsize=(13, 12), sharex=False, facecolor="#121212")
    fig.suptitle(
        "Nuanic Ring: Physiological Telemetry",
        color="white",
        fontsize=16,
        fontweight="bold",
    )
    for ax_array in axes:
        for ax in ax_array:
            ax.set_facecolor("#1e1e1e")

    ax_raw = axes[0][0]
    ax_eda = axes[0][1]
    ax_arousal = axes[1][0]
    ax_imu = axes[1][1]
    ax_summary = axes[2][0]
    ax_empty = axes[2][1]
    ax_empty.axis("off")

    (line_raw,) = ax_raw.plot([], [], lw=1.2, color="#BBBBBB")
    (line_eda,) = ax_eda.plot([], [], lw=1.8, color="#00ffff")
    (line_arousal,) = ax_arousal.plot([], [], lw=1.8, color="#FFD700")
    (line_imu,) = ax_imu.plot([], [], lw=1.5, color="#ff00ff")

    ax_raw.set_title("Raw EDA (ADC Count)", color="#BBBBBB")
    ax_eda.set_title("Filtered Conductance (uS)", color="#00ffff")
    ax_arousal.set_title("Moodmetric Arousal Score (1-100)", color="#FFD700")
    ax_imu.set_title("IMU Motion Intensity", color="#ff00ff")
    ax_summary.set_title("Physiological Summary", color="lightgray")

    for axis in [ax_raw, ax_eda, ax_arousal, ax_imu]:
        axis.set_xlabel("Packet index", color="gray", fontsize=9)
        axis.set_ylabel("Value", color="gray", fontsize=9)
        axis.grid(True, linestyle="--", alpha=0.2, color="lightgray")
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
        axis.spines["left"].set_color("#444444")
        axis.spines["bottom"].set_color("#444444")
        axis.tick_params(colors="silver", labelsize=8)

    ax_summary.axis("off")
    summary_text = ax_summary.text(
        0.01,
        0.98,
        "Initializing scoring pipeline...",
        transform=ax_summary.transAxes,
        va="top",
        ha="left",
        fontsize=12,
        family="monospace",
        color="lightgray",
    )

    status_text = fig.text(
        0.01, 0.975, "Waiting for packets...", fontsize=10, color="white"
    )
    max_points = max(200, window_seconds * 100)

    plt.tight_layout()
    fig.show()
    fig.canvas.draw()

    try:
        while viewer._running:
            if not plt.fignum_exists(fig.number):
                break
            with viewer.state.lock:
                dna_x = list(viewer.state.live_dna_index)[-max_points:]

                imu_x = list(viewer.state.imu_index)[-max_points:]
                imu_y = list(viewer.state.imu_intensity)[-max_points:]

                eda_y = list(viewer.state.mm_filtered_us_wave)[-max_points:]
                arousal_y = list(viewer.state.mm_arousal_wave)[-max_points:]

                w2_raw = list(viewer.state.live_dna_word2)[-max_points:]

                live_dna_packets = viewer.state.live_dna_packets
                imu_packets = viewer.state.imu_packets
                cal_remaining = viewer.state.mm_calibration_remaining
                calibrated = viewer.state.mm_calibrated

            _autoscale_axis(ax_raw, line_raw, dna_x, w2_raw, smooth_window)
            _autoscale_axis(ax_eda, line_eda, dna_x, eda_y, smooth_window)
            _autoscale_axis(ax_arousal, line_arousal, dna_x, arousal_y, smooth_window)
            _autoscale_axis(ax_imu, line_imu, imu_x, imu_y, smooth_window)

            latest_eda_us = eda_y[-1] if eda_y else 0.0
            latest_arousal = arousal_y[-1] if arousal_y else 0.0
            latest_imu_val = imu_y[-1] if imu_y else 0.0

            cal_status = (
                f"CALIBRATING ({cal_remaining:.0f}s left)"
                if not calibrated
                else "CALIBRATED (Active)"
            )

            summary_text.set_text(
                "Physiological Summary\n"
                "----------------------------\n"
                f"Status:    {cal_status}\n"
                f"Arousal:   {latest_arousal:.1f}/100\n"
                f"Conduct.:  {latest_eda_us:.4f} uS\n"
                f"Motion:    {latest_imu_val:.1f} intensity\n\n"
                f"D306 Pkts: {live_dna_packets}\n"
                f"IMU Pkts:  {imu_packets}"
            )

            status_text.set_text(
                f"LIVE MONITOR | Arousal: {latest_arousal:.1f} | Calibrated: {calibrated}"
            )

            fig.canvas.draw_idle()
            plt.pause(refresh_ms / 1000.0)

    except Exception as e:
        print(f"[ERROR] Plotting loop crash: {e}")
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
    calibration_seconds: int = 60,
    target_hz: float | None = None,
    attempt_rate_control: bool = False,
    raw_signal: bool = False,
) -> int:
    """Run standalone live telemetry plotter.

    matplotlib runs in a thread pool (``run_in_executor``) so its GUI event
    loop never conflicts with the asyncio event loop handling BLE callbacks.
    Shared state is protected by ``threading.Lock``.
    """
    viewer = NuanicWaveformViewer(
        ring_addr=ring_addr,
        calibration_seconds=calibration_seconds,
        target_hz=target_hz,
        attempt_rate_control=attempt_rate_control,
        raw_signal=raw_signal,
    )

    if not await viewer.connect_and_subscribe():
        print(
            "[FAIL] Could not connect and subscribe to high-frequency telemetry streams"
        )
        return 1

    print("[OK] Connected. Opening live telemetry window...")
    if smooth_window > 1:
        print(f"[SMOOTH] Applying {smooth_window}-point moving average filter")

    worker = asyncio.create_task(viewer.run_until_stopped())

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            _run_plot_blocking,
            viewer,
            window_seconds,
            refresh_ms,
            smooth_window,
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


def run_waveform_viewer_sync(
    ring_addr: str | None = None,
    window_seconds: int = 10,
    refresh_ms: int = 120,
    smooth_window: int = 1,
    calibration_seconds: int = 60,
    target_hz: float | None = None,
    attempt_rate_control: bool = False,
    raw_signal: bool = False,
) -> int:
    """Run standalone live telemetry plotter using threads.

    The Matplotlib GUI runs on the main thread, while the BLE subscriptions and
    asyncio event loop run on a background daemon thread.
    """
    viewer = NuanicWaveformViewer(
        ring_addr=ring_addr,
        calibration_seconds=calibration_seconds,
        target_hz=target_hz,
        attempt_rate_control=attempt_rate_control,
        raw_signal=raw_signal,
    )

    loop = asyncio.new_event_loop()
    connection_success = threading.Event()
    connection_failed = threading.Event()

    async def async_worker():
        try:
            if not await viewer.connect_and_subscribe():
                connection_failed.set()
                return
            connection_success.set()
            await viewer.run_until_stopped()
        except Exception as e:
            print(f"[ERROR] Async worker exception: {e}")
            connection_failed.set()
        finally:
            await viewer.stop()

    def run_loop():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(async_worker())
        finally:
            loop.close()

    bg_thread = threading.Thread(target=run_loop, name="NuanicBLEWorker", daemon=True)
    bg_thread.start()

    print("[SCAN] Connecting and subscribing to streams in background...")
    while not connection_success.is_set() and not connection_failed.is_set():
        time.sleep(0.1)
        if not bg_thread.is_alive():
            break

    if connection_failed.is_set() or not connection_success.is_set():
        print(
            "[FAIL] Could not connect and subscribe to high-frequency telemetry streams"
        )
        viewer._running = False
        if loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        bg_thread.join(timeout=2.0)
        return 1

    print("[OK] Connected. Opening live telemetry window on main thread...")
    if smooth_window > 1:
        print(f"[SMOOTH] Applying {smooth_window}-point moving average filter")

    try:
        _run_plot_blocking(
            viewer,
            window_seconds,
            refresh_ms,
            smooth_window,
        )
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted by user")
    finally:
        viewer._running = False
        bg_thread.join(timeout=3.0)
        if bg_thread.is_alive():
            print("[WARN] Worker thread did not exit cleanly, forcing loop stop...")
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
            bg_thread.join(timeout=1.0)

    print("[STOP] Waveform viewer stopped")
    return 0
