"""Goals, preferences, proposal review, and approved memory control."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from personal_ai_os.contracts import GoalCreate, HabitCheckin, HabitCreate
from personal_ai_os.display_zh import zh, zh_explanation
from personal_ai_os.services import PersonalAIService
from .common import local_text, parse_datetime, show_error


def render(service: PersonalAIService) -> None:
    st.title("记忆与设置")
    st.caption("管理个人规则、目标与习惯；每条长期记忆都由你审核。")
    first, second, third = st.columns(3)
    first.metric("进行中的目标", sum(goal["status"] == "active" for goal in service.list_goals()))
    second.metric("习惯", len(service.list_habits()))
    third.metric("待审核记忆", len(service.list_memory_proposals("pending")))
    settings_tab, goals_tab, memory_tab = st.tabs(["个人与设备", "目标与习惯", "长期记忆"])
    with settings_tab:
        _render_settings(service)
    with goals_tab:
        _render_goals_habits(service)
    with memory_tab:
        _render_memory(service)


def _render_settings(service: PersonalAIService) -> None:
    settings = service.list_settings()
    st.subheader("个人设置")
    with st.form("settings"):
        timezone_name = st.text_input("时区", settings.get("timezone", service.timezone))
        sleep_start = st.text_input("通常入睡时间（HH:MM）", settings.get("sleep_start", "23:00"))
        sleep_end = st.text_input("通常起床时间（HH:MM）", settings.get("sleep_end", "07:00"))
        personal_goal = st.text_input("当前个人目标", settings.get("personal_goal", ""))
        study_preference = st.text_input("学习偏好备注", settings.get("study_preference", ""))
        save_settings = st.form_submit_button("保存设置")
    if save_settings:
        try:
            service.set_timezone(timezone_name)
            for key, value in (
                ("sleep_start", sleep_start), ("sleep_end", sleep_end),
                ("personal_goal", personal_goal), ("study_preference", study_preference),
            ):
                service.set_preference(key, value)
            st.rerun()
        except Exception as exc:
            show_error(exc)

    st.subheader("提醒设置")
    notification_settings = service.notification_preferences()
    with st.form("notification_settings"):
        quiet_start = st.text_input("静默开始（HH:MM）", notification_settings["quiet_start"])
        quiet_end = st.text_input("静默结束（HH:MM）", notification_settings["quiet_end"])
        system_enabled = st.checkbox("显式开启本机系统通知", value=notification_settings["system_notifications_enabled"])
        save_notifications = st.form_submit_button("保存提醒偏好")
    if save_notifications:
        try:
            service.set_notification_preferences(quiet_start, quiet_end, system_enabled)
            st.rerun()
        except Exception as exc:
            show_error(exc)

    st.subheader("设备授权与手机入口")
    st.caption("设备凭据可用于本机只读视图及独立手机入口；手机入口须另行启动并经私人 HTTPS 连接，"
               "不会开放原 Streamlit 端口。凭据只在创建时显示一次，数据库仅保存哈希；撤销后会话立即失效。")
    with st.form("new_remote_device"):
        device_name = st.text_input("新设备名称")
        device_days = st.number_input("设备凭据有效天数", min_value=1, max_value=90, value=30)
        add_device = st.form_submit_button("签发设备凭据")
    if add_device:
        try:
            issued = service.create_remote_device(device_name, int(device_days))
            st.success("设备已授权。请现在复制凭据；离开此页面后不再显示。")
            st.code(issued["token"])
        except Exception as exc:
            show_error(exc)
    devices = service.list_remote_devices()
    for device in devices:
        with st.expander(f"{device['name']} · {zh(device['status'])}"):
            st.write(f"到期：{device['expires_at_utc']} · 最近使用：{device['last_used_at_utc'] or '从未'}")
            st.write("远端通知：" + ("已开启" if device["notifications_enabled"] else "关闭"))
            if device["status"] == "active":
                subscription = service.remote_notification_subscription(device["id"])
                if subscription:
                    st.caption("在 iPhone 的 ntfy App 中订阅下方主题，服务器为 ntfy.sh。"
                               "主题相当于私密地址，请勿分享；公共服务会暂存通用提醒。")
                    st.code(subscription["topic"])
                    st.caption("推送预览：" + subscription["title"] + " · " + subscription["message"])
                enabled = bool(device["notifications_enabled"])
                if st.button("关闭此设备通知" if enabled else "开启此设备通知",
                             key=f"remote_notice_{device['id']}",
                             disabled=not enabled and not service.remote_notifications_available):
                    try:
                        service.set_remote_device_notifications(device["id"], device["version"], not enabled)
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
                if st.button("撤销此设备", key=f"revoke_device_{device['id']}"):
                    try:
                        service.revoke_remote_device(device["id"], device["version"])
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
    if not service.remote_notifications_available:
        st.caption("尚未选择远端通知渠道；无法开启设备通知。本机提醒继续按原设置运行。")
    deliveries = service.list_remote_deliveries()
    if deliveries:
        st.dataframe([{
            "设备": item["device_id"], "提醒": item["notification_id"],
            "状态": zh(item["status"]), "错误": zh(item["error_code"]),
            "更新时间": item["updated_at_utc"],
        } for item in deliveries], hide_index=True)

    st.subheader("模型用量与本机导出")
    prices = service.model_prices()
    with st.form("model_prices"):
        input_price = st.text_input("每百万输入 token 的美元单价", prices.get("input_per_million", ""))
        output_price = st.text_input("每百万输出 token 的美元单价", prices.get("output_per_million", ""))
        save_prices = st.form_submit_button("保存估算单价")
    if save_prices:
        try:
            service.set_model_prices(input_price, output_price)
            st.rerun()
        except Exception as exc:
            show_error(exc)
    st.caption("单价由你填写；估算成本只覆盖有真实 token 数据的请求，不等于 DeepSeek 账单。")
    left, right = st.columns(2)
    with left:
        st.download_button("导出本机数据 JSON", service.export_data("json"),
                           file_name="personal-ai-os.json", mime="application/json")
    with right:
        st.download_button("导出任务 CSV", service.export_data("tasks_csv"),
                           file_name="personal-ai-os-tasks.csv", mime="text/csv")

def _render_goals_habits(service: PersonalAIService) -> None:
    st.subheader("目标")
    goals = service.list_goals()
    if goals:
        st.dataframe([
            {"目标": goal["title"], "状态": zh(goal["status"]),
             "期限": local_text(goal["due_at_utc"], service.timezone)}
            for goal in goals
        ], hide_index=True)
    with st.form("new_goal"):
        title = st.text_input("新目标标题")
        description = st.text_area("目标说明")
        due = st.text_input("目标期限（ISO 8601，可留空）")
        add_goal = st.form_submit_button("添加目标")
    if add_goal:
        try:
            service.create_goal(GoalCreate(
                title=title, description=description, due_at=parse_datetime(due)
            ))
            st.rerun()
        except Exception as exc:
            show_error(exc)
    if goals:
        goal_id = st.selectbox(
            "选择目标编辑", [goal["id"] for goal in goals],
            format_func=lambda item: next(goal["title"] for goal in goals if goal["id"] == item),
        )
        goal = next(item for item in goals if item["id"] == goal_id)
        with st.form(f"edit_goal_{goal_id}"):
            title = st.text_input("修改目标标题", goal["title"])
            description = st.text_area("修改目标说明", goal["description"])
            due = st.text_input("修改目标期限", local_text(goal["due_at_utc"], service.timezone))
            status = st.selectbox(
                "目标状态", ["active", "completed", "archived"],
                index=["active", "completed", "archived"].index(goal["status"]),
                format_func=zh,
            )
            edit_goal = st.form_submit_button("保存目标")
        if edit_goal:
            try:
                service.update_goal(goal_id, GoalCreate(
                    title=title, description=description, due_at=parse_datetime(due)
                ), status)
                st.rerun()
            except Exception as exc:
                show_error(exc)
        if st.button("删除目标", key=f"delete_goal_{goal_id}"):
            try:
                service.delete_goal(goal_id)
                st.rerun()
            except Exception as exc:
                show_error(exc)

    st.subheader("习惯与打卡")
    with st.form("new_habit"):
        habit_title = st.text_input("新习惯名称")
        habit_zone = st.text_input("习惯时区", service.timezone)
        target = st.number_input("每周目标次数", min_value=1, max_value=7, value=7)
        add_habit = st.form_submit_button("创建习惯")
    if add_habit:
        try:
            service.create_habit(HabitCreate(
                title=habit_title, timezone=habit_zone, target_per_week=target
            ))
            st.rerun()
        except Exception as exc:
            show_error(exc)
    for habit in service.list_habits():
        with st.expander(f"{habit['title']} · {zh(habit['status'])} · {habit['timezone']}"):
            today = datetime.now(ZoneInfo(habit["timezone"])).date()
            checks = service.list_habit_checkins(habit["id"])
            week_start = today - timedelta(days=today.weekday())
            done = sum(
                row["completed"] for row in checks
                if week_start.isoformat() <= row["local_date"] <= today.isoformat()
            )
            st.write(f"本周进度：{done}/{habit['target_per_week']}")
            with st.form(f"edit_habit_{habit['id']}"):
                title = st.text_input("修改习惯名称", habit["title"])
                zone_name = st.text_input("修改习惯时区", habit["timezone"])
                weekly_target = st.number_input("修改每周目标次数", min_value=1, max_value=7,
                                                value=habit["target_per_week"])
                save_habit = st.form_submit_button("保存习惯")
            if save_habit:
                try:
                    service.update_habit(habit["id"], HabitCreate(
                        title=title, timezone=zone_name, target_per_week=weekly_target
                    ))
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            next_status = "paused" if habit["status"] == "active" else "active"
            if st.button("暂停习惯" if next_status == "paused" else "恢复习惯",
                         key=f"habit_status_{habit['id']}"):
                try:
                    service.set_habit_status(habit["id"], next_status)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            with st.form(f"checkin_{habit['id']}"):
                day_text = st.text_input("打卡当地日期（YYYY-MM-DD）", today.isoformat())
                completed = st.checkbox("已完成", value=True)
                note = st.text_input("打卡备注")
                save_checkin = st.form_submit_button("保存或更正打卡", disabled=habit["status"] != "active")
            if save_checkin:
                try:
                    service.checkin_habit(habit["id"], HabitCheckin(
                        local_date=date.fromisoformat(day_text), completed=completed, note=note
                    ))
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            if checks:
                st.dataframe([{"当地日期": row["local_date"], "完成": bool(row["completed"]),
                               "备注": row["note"]} for row in checks], hide_index=True)

def _render_memory(service: PersonalAIService) -> None:
    st.subheader("待审核候选记忆")
    pending = service.list_memory_proposals("pending")
    feedback_by_id = {item["id"]: item for item in service.list_feedback()}
    if not pending:
        st.caption("没有待审核候选。")
    for proposal in pending:
        with st.expander(f"{zh(proposal['kind'])} · {proposal['id'][:8]}"):
            feedback = feedback_by_id.get(proposal["feedback_id"], {})
            st.write("来源反馈：" + feedback.get("body", ""))
            st.write("来源片段：" + proposal["source_excerpt"])
            st.write("提取理由：" + zh_explanation(proposal["explanation"]))
            value = json.loads(proposal["value_json"])
            with st.form(f"proposal_{proposal['id']}"):
                edited_value = _value_form(proposal["kind"], value, "候选")
                save = st.form_submit_button("保存候选修改")
            if save:
                try:
                    service.edit_memory_proposal(proposal["id"], edited_value)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            left, right = st.columns(2)
            with left:
                if st.button("批准记忆", key=f"approve_memory_{proposal['id']}"):
                    try:
                        service.resolve_memory(proposal["id"], approve=True)
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)
            with right:
                if st.button("拒绝候选", key=f"reject_memory_{proposal['id']}"):
                    try:
                        service.resolve_memory(proposal["id"], approve=False)
                        st.rerun()
                    except Exception as exc:
                        show_error(exc)

    st.subheader("已批准长期记忆")
    memories = service.list_memories()
    if not memories:
        st.caption("尚无已批准记忆。")
    for memory in memories:
        with st.expander(f"{zh(memory['kind'])} · {memory['id'][:8]}"):
            source = feedback_by_id.get(memory["source_feedback_id"], {})
            st.write("来源反馈：" + source.get("body", "人工创建"))
            usage = sum(
                memory["id"] in json.loads(run["draft_json"]).get("memory_ids", [])
                for run in service.list_runs()
                if run["kind"] == "planning" and run["draft_json"]
            )
            st.caption(f"被计划引用 {usage} 次")
            value = json.loads(memory["value_json"])
            with st.form(f"memory_{memory['id']}"):
                edited_value = _value_form(memory["kind"], value, "记忆")
                save = st.form_submit_button("保存记忆修改")
            if save:
                try:
                    service.update_memory(memory["id"], edited_value)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            if st.button("删除记忆", key=f"delete_memory_{memory['id']}"):
                try:
                    service.delete_memory(memory["id"])
                    st.rerun()
                except Exception as exc:
                    show_error(exc)


def _value_form(kind: str, value: dict, label: str) -> dict:
    if kind == "study_time_avoid":
        start = st.text_input(f"{label}避让开始（HH:MM）", value.get("start", "00:00"))
        end = st.text_input(f"{label}避让结束（HH:MM）", value.get("end", "12:00"))
        return {"start": start, "end": end}
    text = st.text_area(f"{label}偏好文本", value.get("text", ""))
    return {"text": text}
