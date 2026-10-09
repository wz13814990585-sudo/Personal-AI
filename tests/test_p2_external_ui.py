"""Existing pages expose the real Service directory, previews and audit."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.external import ExternalGateway
from test_p2_external import CalendarFake, event


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_external_preview_each_approval_and_audit_in_existing_pages(flow_system, monkeypatch):
    repo, _, _, _, _, service = flow_system
    gateway: ExternalGateway = service.external
    server = gateway.register_server("Mock calendar", "calendar")
    fake = CalendarFake()
    gateway.adapters[server["id"]] = fake
    server = gateway.set_server_status(server["id"], server["version"], True)
    server = gateway.set_operation_enabled(server["id"], "create_event", server["version"], True)
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)

    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception
    field(at.text_input, "目标账号").set_value("me@example.com")
    field(at.text_input, "目标日历／事件／收件人").set_value("main")
    field(at.text_area, "拟写入内容 JSON").set_value(__import__("json").dumps(event()))
    field(at.button, "生成待确认预览").click().run(timeout=30)
    assert not at.exception
    proposal = gateway.list_proposals()[0]
    assert proposal["status"] == "pending" and fake.calls == []
    assert any("外部写入逐项确认" in item.value for item in at.markdown)
    assert any("main" in item.label for item in at.expander)
    field(at.button, "确认这一项外部写入").click().run(timeout=30)
    assert not at.exception
    assert gateway.get_proposal(proposal["id"])["status"] == "succeeded"
    assert len(fake.calls) == 1
    at.switch_page("pages/04_trace.py").run(timeout=30)
    assert not at.exception
    assert any("外部工具审计" in item.value for item in at.markdown)
    assert any(item["event_type"] == "write_succeeded" for item in service.list_external_audit())


def test_directory_without_provider_is_visible_but_cannot_call(flow_system, monkeypatch):
    _, _, _, _, _, service = flow_system
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    field(at.text_input, "服务器名称").set_value("Unbound mail")
    field(at.selectbox, "服务器类型").set_value("mail")
    field(at.button, "登记暂停的服务器").click().run(timeout=30)
    assert not at.exception
    server = service.list_external_servers()[0]
    assert server["status"] == "paused" and not server["bound"]
    assert any("未绑定" in item.value for item in at.caption)
    assert not any(item.label == "生成待确认预览" for item in at.button)
