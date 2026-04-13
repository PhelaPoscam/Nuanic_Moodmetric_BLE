from nuanic_ring.cli_entrypoints import _script_path


def test_entrypoint_script_paths_exist_in_repo_checkout():
    assert _script_path("ring_monitor_cli.py").exists()
    assert _script_path("ring_analyzer_cli.py").exists()
    assert _script_path("ring_post_analysis_cli.py").exists()
    assert _script_path("discover_ring_services.py").exists()
