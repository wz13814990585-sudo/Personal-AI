"""Device enrollment and revocation stay inside the existing settings page."""

from pathlib import Path

from streamlit.testing.v1 import AppTest
from test_p2_remote import FakeNotifier
from personal_ai_os.ntfy_notifications import MESSAGE, NtfyNotifier


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_device_once_only_token_and_revoke_in_settings(flow_system, monkeypatch):
    _, _, _, _, _, service = flow_system
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/05_memory.py").run(timeout=30)
    assert not at.exception
    assert any("私人 HTTPS" in item.value for item in at.caption)
    field(at.text_input, "新设备名称").set_value("My phone")
    field(at.button, "签发设备凭据").click().run(timeout=30)
    assert not at.exception
    devices = service.list_remote_devices()
    assert len(devices) == 1 and devices[0]["name"] == "My phone"
    assert any("pad_" in item.value for item in at.code)
    assert field(at.button, "开启此设备通知").disabled
    at.run(timeout=30)
    assert not any("pad_" in item.value for item in at.code)
    field(at.button, "撤销此设备").click().run(timeout=30)
    assert not at.exception
    assert service.list_remote_devices()[0]["status"] == "revoked"
    assert not any(item.label == "撤销此设备" for item in at.button)


def test_explicit_device_notification_toggle_in_settings(flow_system, monkeypatch):
    _, _, _, _, _, service = flow_system
    service.remote_notifier = FakeNotifier()
    issued = service.create_remote_device("Tablet")
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/05_memory.py").run(timeout=30)
    assert not at.exception
    assert not service.list_remote_devices()[0]["notifications_enabled"]
    field(at.button, "开启此设备通知").click().run(timeout=30)
    assert not at.exception
    assert service.list_remote_devices()[0]["notifications_enabled"] == 1
    field(at.button, "关闭此设备通知").click().run(timeout=30)
    assert not at.exception
    assert service.list_remote_devices()[0]["notifications_enabled"] == 0
    assert service.list_remote_devices()[0]["id"] == issued["device"]["id"]


def test_ntfy_subscription_preview_and_opt_in_stay_on_local_settings(flow_system, monkeypatch):
    _, _, _, _, _, service = flow_system
    service.remote_notifier = NtfyNotifier("ab" * 32)
    issued = service.create_remote_device("iPhone")
    device = issued["device"]
    topic = service.remote_notification_subscription(device["id"])["topic"]
    assert topic not in service.export_data("json").decode()
    assert topic not in str(service.list_trace())
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/05_memory.py").run(timeout=30)
    assert not at.exception
    assert any(item.value == topic for item in at.code)
    assert any(MESSAGE in item.value for item in at.caption)
    assert service.list_remote_devices()[0]["notifications_enabled"] == 0
    field(at.button, "开启此设备通知").click().run(timeout=30)
    assert service.list_remote_devices()[0]["notifications_enabled"] == 1
    field(at.button, "撤销此设备").click().run(timeout=30)
    assert service.remote_notification_subscription(device["id"]) is None
    assert not any(item.value == topic for item in at.code)
