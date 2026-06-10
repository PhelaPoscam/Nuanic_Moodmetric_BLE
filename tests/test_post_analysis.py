import sys
from pathlib import Path
import tempfile
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nuanic_ring.post_analysis import analyze_latest_ring_logs

def test_analyze_latest_ring_logs_file_selection():
    with tempfile.TemporaryDirectory() as tmpdir:
        log_dir = Path(tmpdir)

        # Create dummy CSV contents with headers
        dummy_df = pd.DataFrame({
            "timestamp": ["2026-06-10T09:00:00.000"],
            "elapsed_ms": [1000],
            "device_mac": ["AA:BB:CC:DD:EE:FF"],
            "connection_state": ["connected"],
            "data_type": ["D306_EDA"],
            "MM_Arousal_Score": [50.0],
            "Stress_Index": [45],
            "MM_Calibrated": [1],
            "D306_Observed_Hz": [16.0],
            "IMU_Observed_Hz": [1.0]
        })

        # File names to test
        legacy_file = log_dir / "ring_AA-BB-CC-DD-EE-FF_2026-06-10_08-00-00.csv"
        new_combined_file = log_dir / "SessionDate_10-06-2026_09-00-00_P01_ring-E34502.csv"
        streamed_file = log_dir / "SessionDate_10-06-2026_09-00-00_P01_ring-E34502_streamed.csv"
        computed_file = log_dir / "SessionDate_10-06-2026_09-00-00_P01_ring-E34502_computed.csv"
        other_file = log_dir / "random_file.csv"

        # Write files
        dummy_df.to_csv(legacy_file, index=False)
        dummy_df.to_csv(new_combined_file, index=False)
        dummy_df.to_csv(streamed_file, index=False)
        dummy_df.to_csv(computed_file, index=False)
        dummy_df.to_csv(other_file, index=False)

        # Run analysis (should fetch latest 2)
        results = analyze_latest_ring_logs(log_dir=str(log_dir), latest_n=5)

        # Should only find the legacy file and new combined file (total 2)
        assert len(results) == 2
        file_names = {r.path.name for r in results}
        assert legacy_file.name in file_names
        assert new_combined_file.name in file_names
        assert streamed_file.name not in file_names
        assert computed_file.name not in file_names
        assert other_file.name not in file_names

        # Verify parsed stats
        for res in results:
            assert res.samples_used == 1
            assert res.correlation is not None
