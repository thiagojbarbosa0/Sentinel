import os
import platform
import subprocess
import time

import psutil
import pytest

from app import automation
from app.config import MANAGED_TEMP_DIR


def test_cleanup_only_removes_files_older_than_cutoff():
    MANAGED_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    old_file = MANAGED_TEMP_DIR / "old.log"
    new_file = MANAGED_TEMP_DIR / "new.log"
    old_file.write_text("x" * 1024)
    new_file.write_text("y" * 512)

    old_ts = time.time() - 3600 * 5
    os.utime(old_file, (old_ts, old_ts))  # arquivo "novo" mantém mtime atual

    result = automation.action_cleanup_temp_files(max_age_hours=1)

    assert result["status"] == "executed"
    assert result["after"]["removed_count"] == 1
    assert result["after"]["freed_bytes"] == 1024
    assert not old_file.exists()
    assert new_file.exists(), "arquivo recente não deveria ser apagado"
    new_file.unlink()


def test_cleanup_never_touches_files_outside_managed_dir(tmp_path):
    outside_file = tmp_path / "should_not_be_touched.log"
    outside_file.write_text("sensitive")
    result = automation.action_cleanup_temp_files(max_age_hours=0)
    assert result["status"] == "executed"
    assert outside_file.exists(), "ação nunca deve apagar nada fora do diretório gerenciado"


def test_flag_for_review_never_modifies_anything():
    result = automation.action_flag_for_review("suspicious-process")
    assert result["status"] == "executed"
    assert result["before"] is None
    assert result["rollback"] is None
    assert result["after"]["flagged"] == "suspicious-process"


def test_run_network_diagnostic_is_read_only(monkeypatch):
    monkeypatch.setattr(
        "app.collectors.collect_network_latency",
        lambda: {"gateway_host": "10.0.0.1", "gateway_latency_ms": 2.0,
                 "dns_latency_ms": 4.0, "internet_latency_ms": 20.0},
    )
    result = automation.action_run_network_diagnostic()
    assert result["status"] == "executed"
    assert result["before"] is None
    assert result["after"]["internet_latency_ms"] == 20.0


def test_unknown_action_fails_gracefully():
    result = automation.execute_action("delete_everything", "x")
    assert result["status"] == "failed"
    assert "desconhecida" in result["error"]


def test_change_priority_reports_failure_when_process_not_found():
    result = automation.action_change_process_priority("this-process-does-not-exist-xyz")
    assert result["status"] == "failed"
    assert "Nenhum processo" in result["error"]


@pytest.mark.skipif(platform.system() == "Windows", reason="semântica de nice() difere no Windows")
def test_change_priority_and_rollback_on_real_process():
    proc_name = "sleep"
    child = subprocess.Popen([proc_name, "20"])
    try:
        time.sleep(0.3)
        original_nice = psutil.Process(child.pid).nice()

        result = automation.action_change_process_priority(proc_name, delta=5)
        assert result["status"] == "executed"
        assert psutil.Process(child.pid).nice() == min(original_nice + 5, 19)
        assert result["rollback"]["action"] == "change_process_priority"

        rollback_result = automation.rollback_action("change_process_priority", result["rollback"])
        assert rollback_result["status"] == "executed"
        assert psutil.Process(child.pid).nice() == original_nice
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_rollback_unsupported_action_is_explicit():
    result = automation.rollback_action("cleanup_temp_files", {})
    assert result["status"] == "failed"
    assert "não é reversível" in result["error"]
