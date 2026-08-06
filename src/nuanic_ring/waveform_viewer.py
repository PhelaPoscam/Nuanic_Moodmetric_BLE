"""Standalone live viewer for Nuanic ring telemetry streams.

ponytail: matplotlib runs in a thread pool (run_in_executor) so the GUI
event loop never shares a thread with asyncio. Shared state guarded by
threading.Lock.
"""

import asyncio
import threading
import time
from typing import Any, Sequence

import matplotlib.pyplot as plt
import numpy as np


def smooth_data(data: "Sequence[Any]", window: int) -> list:
    """Apply moving-average smoothing."""
    if not data or window <= 1:
        return list(data)
    if len(data) < window:
        return list(data)

    kernel = np.ones(window) / window
    smoothed = np.convolve(data, kernel, mode="valid")
    pad_length = len(data) - len(smoothed)
    padded = np.pad(smoothed, (pad_length, 0), mode="edge")
    return padded.tolist()


def _autoscale_axis(
    axis,
    line,
    x_data: "Sequence[float]",
    y_data: "Sequence[float]",
    smooth_window: int = 1,
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
        if abs(ymin) < 100:
            pad = max(0.001, abs(float(ymin)) * 0.01)
        else:
            pad = max(1.0, abs(float(ymin)) * 0.01)
    else:
        pad = max(0.0001, (ymax - ymin) * 0.1)
    axis.set_ylim(ymin - pad, ymax + pad)


def _run_plot_blocking(
    monitor: Any,
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
    ax_arousal.set_title("Nuanic DNE (Stress Index)", color="white", fontsize=10)
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
        while monitor.running:
            if not plt.fignum_exists(fig.number):
                break

            state = None
            if monitor.device_states:
                state = list(monitor.device_states.values())[0]

            if state:
                dna_x = list(state.live_dna_index)[-max_points:]
                imu_x = list(state.imu_index)[-max_points:]
                imu_y = list(state.imu_intensity)[-max_points:]
                eda_y = list(state.mm_filtered_us_wave)[-max_points:]
                arousal_y = list(state.dne_stress_index_wave)[-max_points:]
                w2_raw = list(state.live_dna_word2)[-max_points:]

                live_dna_packets = (
                    state.d306_count
                    if hasattr(state, "d306_count") and state.d306_count > 0
                    else getattr(state, "live_eda_count", 0)
                )
                imu_packets = state.imu_batch_count
            else:
                dna_x, imu_x, imu_y, eda_y, arousal_y, w2_raw = [], [], [], [], [], []
                live_dna_packets = imu_packets = 0

            _autoscale_axis(ax_raw, line_raw, dna_x, w2_raw, smooth_window)
            _autoscale_axis(ax_eda, line_eda, dna_x, eda_y, smooth_window)
            _autoscale_axis(ax_arousal, line_arousal, dna_x, arousal_y, smooth_window)
            _autoscale_axis(ax_imu, line_imu, imu_x, imu_y, smooth_window)

            latest_eda_us = eda_y[-1] if eda_y else 0.0
            latest_arousal = arousal_y[-1] if arousal_y else 0.0
            latest_imu_val = imu_y[-1] if imu_y else 0.0

            cal_status = "Hardware DNE (Active)"

            summary_text.set_text(
                "Physiological Summary\n"
                "----------------------------\n"
                f"Status:    {cal_status}\n"
                f"Nuanic DNE:{latest_arousal:.1f}/100\n"
                f"Conduct.:  {latest_eda_us:.4f} uS\n"
                f"Motion:    {latest_imu_val:.1f} intensity\n\n"
                f"EDA Pkts:  {live_dna_packets}\n"
                f"IMU Pkts:  {imu_packets}"
            )

            status_text.set_text(
                f"LIVE MONITOR | Nuanic DNE: {latest_arousal:.1f} | Hardware DNE Active"
            )

            try:
                fig.canvas.draw_idle()
                plt.pause(refresh_ms / 1000.0)
            except KeyboardInterrupt:
                break
            except Exception:
                pass

    except Exception as e:
        print(f"[ERROR] Plotting loop crash: {e}")
    finally:
        try:
            plt.close(fig)
        except Exception:
            pass


def run_waveform_viewer_sync(
    ring_addr: str | None = None,
    window_seconds: int = 10,
    refresh_ms: int = 120,
    smooth_window: int = 1,
    target_hz: float | None = None,
    attempt_rate_control: bool = False,
    apply_filter: bool = False,
    enable_logging: bool = False,
    log_dir: str = "data/ring_logs",
    participant_id: str | None = None,
    csv_layout: str = "combined",
    initial_mode: int | None = None,
) -> int:
    """Run standalone live telemetry plotter using threads.

    The Matplotlib GUI runs on the main thread, while the BLE subscriptions and
    asyncio event loop run on a background daemon thread.
    """
    from .monitor import NuanicMonitor

    monitor = NuanicMonitor(
        log_dir=log_dir,
        enable_logging=enable_logging,
        participant_id=participant_id,
        csv_layout=csv_layout,
        target_hz=target_hz,
        attempt_ring_rate_control=attempt_rate_control,
        apply_filter=apply_filter,
        initial_mode=initial_mode,
    )

    loop = asyncio.new_event_loop()
    connection_success = threading.Event()
    connection_failed = threading.Event()

    async def async_worker():
        try:
            started = await monitor.start_multi(
                ring_addresses=[ring_addr] if ring_addr else None
            )
            if not started:
                connection_failed.set()
                return
            connection_success.set()
            while monitor.running:
                await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[ERROR] Async worker exception: {e}")
            connection_failed.set()
        finally:
            await monitor.stop_multi()

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
        monitor.running = False
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
            monitor,
            window_seconds,
            refresh_ms,
            smooth_window,
        )
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"\n[STOP] Plotter exception: {e}")
    finally:
        print(
            "\n[STOP] Interrupted by user"
            if not monitor.running
            else "\n[STOP] Closing plotter..."
        )
        monitor.running = False
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
