"""iCloud controls stay on the existing Agent console and call the Service."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from test_p2_icloud import BridgeFake, allow


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_icloud_permission_request_is_explicit_and_selection_persists(flow_system, monkeypatch):
    _, _, _, _, _, service = flow_system
    bridge = BridgeFake()
    service.icloud_bridge = bridge
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception and bridge.calls == []
    field(at.button, "授权并列出 iCloud 日历").click().run(timeout=30)
    assert any(call[0] == "list_calendars" for call in bridge.calls)
    assert field(at.selectbox, "选择 iCloud 日历").value == "icloud-calendar"
    field(at.button, "保存所选 iCloud 日历").click().run(timeout=30)
    assert service.get_icloud_calendar_selection()["calendar_id"] == "icloud-calendar"
    server = next(item for item in service.list_external_servers() if item["kind"] == "calendar")
    assert server["status"] == "paused"
    assert all(not item["enabled"] for item in server["operations"])


def test_icloud_preview_read_update_and_approval(flow_system, monkeypatch):
    _, _, _, _, _, service = flow_system
    bridge = BridgeFake()
    service.icloud_bridge = bridge
    selection = service.select_icloud_calendar("icloud-calendar")
    for operation in ("list_events", "create_event", "update_event"):
        allow(service, selection, operation)
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)

    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception
    assert any("当前目标：iCloud / 个人" in item.value for item in at.info)

    field(at.text_input, "iCloud 事件标题").set_value("复习 EventKit")
    field(at.text_input, "iCloud 开始时间（当地 YYYY-MM-DDTHH:MM）").set_value("2026-10-10T21:00")
    field(at.text_input, "iCloud 结束时间（当地 YYYY-MM-DDTHH:MM）").set_value("2026-10-10T22:00")
    field(at.checkbox, "设置 iCloud 提前提醒").check()
    field(at.number_input, "iCloud 提前提醒分钟").set_value(20)
    field(at.button, "生成 iCloud 新事件草案").click().run(timeout=30)
    assert not at.exception
    proposal = service.list_external_write_proposals()[0]
    assert proposal["status"] == "pending" and proposal["payload"]["reminder_minutes"] == 20
    assert not [call for call in bridge.calls if call[0] == "write_event"]
    assert any("当地时间（Australia/Sydney）" in item.value for item in at.markdown)
    field(at.button, "确认这一项外部写入").click().run(timeout=30)
    assert service.list_external_write_proposals()[0]["status"] == "succeeded"
    assert len([call for call in bridge.calls if call[0] == "write_event"]) == 1

    field(at.button, "读取所选 iCloud 日历").click().run(timeout=30)
    assert not at.exception
    assert field(at.selectbox, "要修改的 iCloud 事件")
    field(at.text_input, "修改后标题").set_value("修改后的事件")
    field(at.button, "生成 iCloud 修改草案").click().run(timeout=30)
    assert not at.exception
    update = service.list_external_write_proposals()[0]
    assert update["status"] == "pending" and update["operation"] == "update_event"
    assert len([call for call in bridge.calls if call[0] == "write_event"]) == 1
    field(at.button, "确认这一项外部写入").click().run(timeout=30)
    assert service.list_external_write_proposals()[0]["status"] == "succeeded"
    assert len([call for call in bridge.calls if call[0] == "write_event"]) == 2
