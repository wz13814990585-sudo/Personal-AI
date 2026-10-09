"""External capabilities and item-by-item write review on the existing console page."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from personal_ai_os.display_zh import zh
from personal_ai_os.services import PersonalAIService
from .common import show_error


def _local(utc_value: str, timezone_name: str) -> str:
    return datetime.fromisoformat(utc_value).astimezone(ZoneInfo(timezone_name)).strftime(
        "%Y-%m-%dT%H:%M"
    )


def _render_icloud(service: PersonalAIService) -> None:
    st.markdown("#### Mac iCloud 日历")
    st.caption("仅在点击下方按钮后请求 macOS 日历权限。选择日历后，还需在目录中逐项启用服务器与读取／写入操作。")
    if st.button("授权并列出 iCloud 日历"):
        try:
            st.session_state["icloud_calendar_choices"] = service.available_icloud_calendars()
        except Exception as exc:
            show_error(exc)
    choices = st.session_state.get("icloud_calendar_choices", [])
    if choices:
        by_id = {item["calendar_id"]: item for item in choices}
        chosen_id = st.selectbox("选择 iCloud 日历", list(by_id),
                                 format_func=lambda key: by_id[key]["source_title"] + " / " + by_id[key]["title"])
        if st.button("保存所选 iCloud 日历"):
            try:
                service.select_icloud_calendar(chosen_id)
                st.rerun()
            except Exception as exc:
                show_error(exc)
    selection = service.get_icloud_calendar_selection()
    if not selection:
        return
    st.info(f"当前目标：{selection['source_title']} / {selection['title']} · 时区：{service.timezone}")
    server = next((item for item in service.list_external_servers()
                   if item["id"] == selection["server_id"]), None)
    can_read = bool(server and server["status"] == "active" and any(
        item["operation"] == "list_events" and item["enabled"]
        for item in server["operations"]
    ))
    if not can_read:
        st.warning("读取尚未启用：请到「外部工具目录」展开 iCloud Calendar，先点「启用服务器」，"
                   "再点「列出日历事件 · 只读」旁的「允许操作」。")
    today = datetime.now(ZoneInfo(service.timezone)).date()
    with st.form("icloud_event_read"):
        first_day = st.date_input("读取起始日期", today)
        last_day = st.date_input("读取结束日期", today + timedelta(days=7))
        do_read = st.form_submit_button("读取所选 iCloud 日历", disabled=not can_read)
    if do_read:
        try:
            st.session_state["icloud_events"] = service.read_icloud_local_days(first_day, last_day)["items"]
        except Exception as exc:
            show_error(exc)
    events = st.session_state.get("icloud_events", [])
    if events:
        st.write("已读取事件（来自所选 iCloud 日历）：")
        for item in events:
            st.write(f"{item['title']} · {_local(item['start_at_utc'], service.timezone)}"
                     f"–{_local(item['end_at_utc'], service.timezone)} · ID {item['event_id']}")
    default_start = datetime.now(ZoneInfo(service.timezone)).replace(
        minute=0, second=0, microsecond=0
    ) + timedelta(hours=1)
    with st.form("icloud_event_create"):
        st.write("创建 iCloud 事件：先生成待审草案")
        title = st.text_input("iCloud 事件标题")
        start_local = st.text_input("iCloud 开始时间（当地 YYYY-MM-DDTHH:MM）",
                                    default_start.strftime("%Y-%m-%dT%H:%M"))
        end_local = st.text_input("iCloud 结束时间（当地 YYYY-MM-DDTHH:MM）",
                                  (default_start + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
        description = st.text_area("iCloud 事件说明")
        location = st.text_input("iCloud 事件地点")
        remind = st.checkbox("设置 iCloud 提前提醒", value=False)
        minutes = st.number_input("iCloud 提前提醒分钟", min_value=0, max_value=10080, value=15)
        prepare = st.form_submit_button("生成 iCloud 新事件草案")
    if prepare:
        try:
            service.propose_icloud_create(title, start_local, end_local, description,
                                          location, int(minutes) if remind else None)
            st.rerun()
        except Exception as exc:
            show_error(exc)
    if events:
        by_event = {item["event_id"]: item for item in events if item.get("event_id")}
        event_id = st.selectbox("要修改的 iCloud 事件", list(by_event),
                                format_func=lambda key: by_event[key]["title"] + " · " + key)
        old = by_event[event_id]
        with st.form(f"icloud_event_update_{event_id}"):
            st.write("修改 iCloud 事件：原内容保留到草案被逐项确认")
            new_title = st.text_input("修改后标题", old["title"])
            new_start = st.text_input("修改后开始时间（当地 YYYY-MM-DDTHH:MM）",
                                      _local(old["start_at_utc"], service.timezone))
            new_end = st.text_input("修改后结束时间（当地 YYYY-MM-DDTHH:MM）",
                                    _local(old["end_at_utc"], service.timezone))
            new_description = st.text_area("修改后说明", old.get("description", ""))
            new_location = st.text_input("修改后地点", old.get("location", ""))
            old_minutes = old.get("reminder_minutes")
            new_remind = st.checkbox("修改后设置提前提醒", value=old_minutes is not None)
            new_minutes = st.number_input("修改后提前提醒分钟", min_value=0,
                                          max_value=10080, value=int(old_minutes or 15))
            prepare_update = st.form_submit_button("生成 iCloud 修改草案")
        if prepare_update:
            try:
                service.propose_icloud_update(event_id, new_title, new_start, new_end,
                                               new_description, new_location,
                                               int(new_minutes) if new_remind else None)
                st.rerun()
            except Exception as exc:
                show_error(exc)


def _render_gmail(service: PersonalAIService) -> None:
    st.markdown("#### Gmail 邮件")
    status = service.gmail_status()
    if not status["configured"]:
        st.caption("先在本机 .env 配置 GMAIL_ACCOUNT 和 GMAIL_OAUTH_CLIENT_PATH；OAuth 客户端文件须仅本人可读。")
        return
    st.info(f"预期授权账号：{status['account']}")
    if not status["connected"]:
        if st.button("连接 Gmail（打开浏览器授权）"):
            try:
                service.connect_gmail()
                st.rerun()
            except Exception as exc:
                show_error(exc)
        return
    st.caption("授权账号已绑定；仍须在下方目录逐项启用 Gmail 服务器和读取／发送操作。邮件正文只在你选中后读取。")
    folder_label = st.selectbox("Gmail 邮件夹", ["收件箱", "已发送"])
    folder = "INBOX" if folder_label == "收件箱" else "SENT"
    page_size = st.selectbox("每页邮件数", [5, 10, 20], index=1)
    if st.button("列出 Gmail 邮件"):
        try:
            st.session_state["gmail_page"] = service.list_gmail_messages(page_size, folder=folder)
            st.session_state["gmail_page_folder"] = folder
            st.session_state.pop("gmail_selected_message", None)
        except Exception as exc:
            show_error(exc)
    page = st.session_state.get("gmail_page")
    if (page and page.get("source", {}).get("account_id") == status["account"]
            and st.session_state.get("gmail_page_folder") == folder):
        messages = page["items"]
        if messages:
            by_id = {item["id"]: item for item in messages}
            message_id = st.selectbox("选择要读取正文的邮件", list(by_id),
                                      format_func=lambda key: (by_id[key].get("subject") or "（无主题）") +
                                      " · " + (by_id[key].get("from") or "") + " · " + key)
            if st.button("读取选中邮件正文"):
                try:
                    st.session_state["gmail_selected_message"] = service.get_gmail_message(message_id)["items"][0]
                except Exception as exc:
                    show_error(exc)
        else:
            st.caption("这一页没有邮件。")
        if page.get("next_page_token") and st.button("Gmail 下一页"):
            try:
                st.session_state["gmail_page"] = service.list_gmail_messages(
                    page_size, page["next_page_token"], folder
                )
                st.session_state.pop("gmail_selected_message", None)
                st.rerun()
            except Exception as exc:
                show_error(exc)
    message = (st.session_state.get("gmail_selected_message")
               if st.session_state.get("gmail_page_folder") == folder else None)
    if message:
        st.write(f"发件人：{message['from']} · 主题：{message['subject']} · 日期：{message['date']}")
        st.text_area("选中邮件正文（只读）", message["body"], disabled=True, height=240)
    with st.form("gmail_send"):
        st.write("发送 Gmail：先生成待审草案")
        recipient = st.text_input("Gmail 测试收件人")
        subject = st.text_input("Gmail 邮件主题")
        body = st.text_area("Gmail 邮件正文")
        prepare = st.form_submit_button("生成 Gmail 发送草案")
    if prepare:
        try:
            service.propose_gmail_send(recipient, subject, body)
            st.rerun()
        except Exception as exc:
            show_error(exc)


def render(service: PersonalAIService) -> None:
    st.subheader("MCP 与外部日历／邮件")
    st.caption("先处理待确认写入，再管理各项连接；目录中的权限必须逐项开启。")
    approval_tab, calendar_tab, mail_tab, youtube_tab, catalog_tab = st.tabs(
        ["待确认写入", "iCloud 日历", "Gmail 邮件", "YouTube", "外部工具目录"]
    )
    with approval_tab:
        _render_proposals(service)
    with calendar_tab:
        _render_icloud(service)
    with mail_tab:
        _render_gmail(service)
    with youtube_tab:
        _render_youtube(service)
    with catalog_tab:
        _render_catalog(service)


def _render_youtube(service: PersonalAIService) -> None:
    st.markdown("#### YouTube 公共学习资料 · 本机 MCP stdio")
    youtube = service.youtube_status()
    st.caption("仅搜索公开视频和读取标题、频道、时长、链接；不读取个人账号数据或字幕。"
               "本次规划还须单独勾选 YouTube，结果先进入待审草案。")
    st.write("API 密钥：" + ("已在本机环境配置" if youtube["api_key_configured"] else "尚未配置 YOUTUBE_API_KEY"))
    if not youtube["server_id"]:
        if st.button("登记 YouTube 本机只读服务器"):
            try:
                service.register_youtube_mcp()
                st.rerun()
            except Exception as exc:
                show_error(exc)
    else:
        st.caption("服务器和两项操作须在「外部工具目录」中逐项启用。")
        with st.form("youtube_search"):
            youtube_query = st.text_input("YouTube 学习资料搜索词")
            youtube_search = st.form_submit_button("搜索 YouTube 公开视频")
        if youtube_search:
            try:
                st.session_state["youtube_results"] = service.search_youtube(youtube_query)["items"]
            except Exception as exc:
                show_error(exc)
        for video in st.session_state.get("youtube_results", []):
            st.link_button(f"{video['title']} · {video['channel']} · "
                           f"{video['duration_seconds'] // 60} 分钟 · {video['source']}",
                           video["url"])
def _render_catalog(service: PersonalAIService) -> None:
    st.caption("目录仅保存本机白名单；未配置适配器时不会发生外部调用。")
    youtube = service.youtube_status()
    with st.form("external_register"):
        name = st.text_input("服务器名称")
        kind = st.selectbox("服务器类型", ["calendar", "mail", "mcp"], format_func=zh)
        operations_json = st.text_area("MCP 操作白名单 JSON（仅 MCP 类型）",
                                       '{"find_notes":"read"}')
        register = st.form_submit_button("登记暂停的服务器")
    if register:
        try:
            service.register_external_server(
                name, kind, json.loads(operations_json) if kind == "mcp" else None
            )
            st.rerun()
        except Exception as exc:
            show_error(exc)

    servers = service.list_external_servers()
    for server in servers:
        with st.expander(f"{server['name']} · {zh(server['kind'])} · {zh(server['status'])}"):
            st.caption("适配器：" + ("已绑定" if server["bound"] else "未绑定；仅目录配置"))
            active = server["status"] == "active"
            if st.button("暂停服务器" if active else "启用服务器",
                         key=f"server_status_{server['id']}"):
                try:
                    service.set_external_server_status(server["id"], server["version"], not active)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            for item in server["operations"]:
                enabled = bool(item["enabled"])
                st.write(f"{zh(item['operation'])} · {zh(item['access'])} · {'允许' if enabled else '关闭'}")
                if st.button("关闭操作" if enabled else "允许操作",
                             key=f"external_op_{server['id']}_{item['operation']}"):
                    try:
                        service.set_external_operation_enabled(
                            server["id"], item["operation"], server["version"], not enabled
                        )
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
            selection = service.get_icloud_calendar_selection()
            gmail = service.gmail_status()
            is_bound_builtin = ((selection and server["id"] == selection["server_id"]) or
                                (gmail["connected"] and server["id"] == gmail["server_id"]) or
                                server["id"] == youtube["server_id"])
            if active and server["bound"] and not is_bound_builtin:
                allowed = [op for op in server["operations"] if op["enabled"]]
                reads = [op["operation"] for op in allowed if op["access"] == "read"]
                writes = [op["operation"] for op in allowed if op["access"] == "write"]
                if reads:
                    with st.form(f"external_read_{server['id']}"):
                        read_op = st.selectbox("读取操作", reads, key=f"read_op_{server['id']}",
                                               format_func=zh)
                        read_account = st.text_input("读取账号", key=f"read_account_{server['id']}")
                        read_query = st.text_area("读取参数 JSON", "{}", key=f"read_query_{server['id']}")
                        do_read = st.form_submit_button("读取外部数据")
                    if do_read:
                        try:
                            st.json(service.read_external(server["id"], read_op, read_account,
                                                          json.loads(read_query)))
                        except Exception as exc:
                            show_error(exc)
                if writes:
                    with st.form(f"external_write_{server['id']}"):
                        write_op = st.selectbox("写入操作", writes, key=f"write_op_{server['id']}",
                                                format_func=zh)
                        account = st.text_input("目标账号", key=f"write_account_{server['id']}")
                        target = st.text_input("目标日历／事件／收件人", key=f"write_target_{server['id']}")
                        payload = st.text_area("拟写入内容 JSON", "{}", key=f"write_payload_{server['id']}")
                        prepare = st.form_submit_button("生成待确认预览")
                    if prepare:
                        try:
                            service.propose_external_write(
                                server["id"], write_op, account, target, json.loads(payload)
                            )
                            st.rerun()
                        except Exception as exc:
                            show_error(exc)

def _render_proposals(service: PersonalAIService) -> None:
    st.markdown("#### 外部写入逐项确认")
    proposals = service.list_external_write_proposals()
    if not proposals:
        st.info("没有待确认的外部写入。创建日历事件或邮件草案后，会先在这里展示完整内容。")
    for proposal in proposals:
        with st.expander(f"{zh(proposal['operation'])} → {proposal['target']} · "
                         f"{zh(proposal['status'])} · {proposal['id'][:8]}"):
            st.write(f"服务器：{proposal['server_id']} · 账号：{proposal['account_id']}")
            selection = service.get_icloud_calendar_selection()
            if selection and proposal["server_id"] == selection["server_id"]:
                payload = proposal["payload"]
                target_calendar = (proposal["target"] if proposal["operation"] == "create_event"
                                   else payload.get("selected_calendar_id"))
                if target_calendar == selection["calendar_id"]:
                    st.write(f"目标日历：{selection['source_title']} / {selection['title']}")
                else:
                    st.warning(f"草案目标日历 ID：{target_calendar}；当前选定日历不同，确认将被拒绝。")
                if "start_at_utc" in payload and "end_at_utc" in payload:
                    st.write(f"当地时间（{service.timezone}）："
                             f"{_local(payload['start_at_utc'], service.timezone)} → "
                             f"{_local(payload['end_at_utc'], service.timezone)}")
                st.write("提前提醒：" + (f"{payload['reminder_minutes']} 分钟"
                                    if payload.get("reminder_minutes") is not None else "无"))
            gmail = service.gmail_status()
            if gmail["connected"] and proposal["server_id"] == gmail["server_id"]:
                st.write(f"发件账号：{proposal['account_id']} · 收件人：{proposal['target']}")
                st.write(f"主题：{proposal['payload'].get('subject', '')}")
                st.text_area("待确认邮件正文", proposal["payload"].get("body", ""),
                             disabled=True, key=f"gmail_preview_{proposal['id']}")
            st.json(proposal["payload"])
            st.caption(f"版本：{proposal['revision']} · 状态：{zh(proposal['status'])}")
            if proposal["error_code"]:
                st.error(zh(proposal["error_code"]))
            if proposal["result"] is not None:
                st.json(proposal["result"])
            if proposal["status"] == "pending":
                if st.button("确认这一项外部写入", type="primary", key=f"external_approve_{proposal['id']}"):
                    try:
                        service.approve_external_write(proposal["id"], proposal["revision"])
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
                if st.button("拒绝这一项外部写入", key=f"external_reject_{proposal['id']}"):
                    try:
                        service.reject_external_write(proposal["id"], proposal["revision"])
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
