"""The dashboard service definition: supervised, restarts, and never tied to a terminal or Claude session."""
import os
import xml.etree.ElementTree as ET

import pytest

from afd_oracle_ui import service


def test_command_runs_the_supervisor():
    cmd = service.command(9999)
    assert cmd[1:] == ["-m", "afd_oracle_ui.serve", "--port", "9999"]
    if os.name == "nt":
        assert cmd[0].lower().endswith("pythonw.exe") or cmd[0].lower().endswith("python.exe")


@pytest.mark.skipif(os.name != "nt", reason="Task Scheduler XML is Windows-only")
def test_task_has_logon_trigger_watchdog_and_no_time_limit():
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(service._task_xml(8765).split("\n", 1)[1])
    assert root.find("t:Triggers/t:LogonTrigger", ns) is not None
    assert root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text == "PT1M"  # watchdog
    s = root.find("t:Settings", ns)
    assert s.find("t:MultipleInstancesPolicy", ns).text == "IgnoreNew"  # watchdog is a no-op while running
    assert s.find("t:ExecutionTimeLimit", ns).text == "PT0S"
    assert s.find("t:DisallowStartIfOnBatteries", ns).text == "false"


def test_unit_and_plist_restart_always():
    assert "Restart=always" in service._unit(8765)
    assert "<key>KeepAlive</key><true/>" in service._plist(8765)


def test_answering_is_false_for_a_closed_port():
    from afd_oracle_ui.serve import answering
    assert answering(1, timeout=0.5) is False
