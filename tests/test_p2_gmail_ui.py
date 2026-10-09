"""Gmail OAuth, selected-body read and reviewed send on the existing console."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.gmail import GmailAdapter
from test_p2_gmail import ACCOUNT, FakeOAuth, FakeTransport, allow, connected


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_gmail_connection_requires_explicit_click(flow_system, monkeypatch):
    _, _, _, _, _, service = flow_system
    transport = FakeTransport()
    oauth = FakeOAuth(transport)
    service.gmail_account = ACCOUNT
    service.gmail_oauth = oauth
    service.gmail_adapter = GmailAdapter(ACCOUNT, oauth)
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception and not service.gmail_status()["connected"]
    assert transport.calls == []
    field(at.button, "连接 Gmail（打开浏览器授权）").click().run(timeout=30)
    assert not at.exception and service.gmail_status()["connected"]
    server = next(item for item in service.list_external_servers() if item["kind"] == "mail")
    assert server["status"] == "paused" and all(not item["enabled"] for item in server["operations"])


def test_gmail_page_selected_read_pagination_and_send_approval(flow_system, monkeypatch):
    _, _, _, service, transport, status = connected(flow_system)
    for operation in ("list_messages", "get_message", "send_message"):
        allow(service, status, operation)
    monkeypatch.setattr("personal_ai_os.bootstrap.page_service", lambda: service)
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/03_agents.py").run(timeout=30)
    assert not at.exception
    field(at.button, "列出 Gmail 邮件").click().run(timeout=30)
    assert not at.exception and field(at.selectbox, "选择要读取正文的邮件").value == "m1"
    assert not any("format=full" in call[1] for call in transport.calls)
    field(at.button, "读取选中邮件正文").click().run(timeout=30)
    assert field(at.text_area, "选中邮件正文（只读）").value == "private incoming body"
    field(at.button, "Gmail 下一页").click().run(timeout=30)
    assert field(at.selectbox, "选择要读取正文的邮件").value == "m2"
    field(at.selectbox, "Gmail 邮件夹").set_value("已发送").run(timeout=30)
    field(at.button, "列出 Gmail 邮件").click().run(timeout=30)
    assert any("labelIds=SENT" in call[1] for call in transport.calls)
    field(at.text_input, "Gmail 测试收件人").set_value("test@example.test")
    field(at.text_input, "Gmail 邮件主题").set_value("Test subject")
    field(at.text_area, "Gmail 邮件正文").set_value("Test body")
    field(at.button, "生成 Gmail 发送草案").click().run(timeout=30)
    assert not at.exception
    proposal = service.list_external_write_proposals()[0]
    assert proposal["status"] == "pending" and transport.sent == 0
    assert field(at.text_area, "待确认邮件正文").value == "Test body"
    assert any("收件人：test@example.test" in item.value for item in at.markdown)
    field(at.button, "确认这一项外部写入").click().run(timeout=30)
    assert service.list_external_write_proposals()[0]["status"] == "succeeded"
    assert transport.sent == 1
