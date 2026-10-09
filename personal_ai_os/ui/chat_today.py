"""Natural-language planning, draft review, and today's tasks."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from personal_ai_os.contracts import TaskDraft
from personal_ai_os.demo import DEMO_REQUEST
from personal_ai_os.display_zh import zh, zh_data, zh_explanation, zh_issue, zh_role, zh_step
from personal_ai_os.services import PersonalAIService
from .common import local_text, parse_datetime, show_error
from .daily_view import render as render_daily_view


def render(service: PersonalAIService) -> None:
    st.title("对话与今日计划")
    if service.demo_mode:
        st.caption("演示路线：生成六 Agent 草案 → 查看执行轨迹 → 确认待办 → "
                   "完成演示任务并提交反馈 → 审核记忆 → 再次规划。")
    if "conversation_id" not in st.session_state:
        st.session_state.conversation_id = uuid4().hex

    active_custom = [agent for agent in service.list_custom_agents()
                     if agent["status"] == "active"]
    selected_ids = st.multiselect(
        "选用自定义 Agent 参与本次规划（只读）",
        [agent["id"] for agent in active_custom],
        format_func=lambda agent_id: next(
            agent["name"] for agent in active_custom if agent["id"] == agent_id
        ),
        max_selections=service.harness.config.max_custom_agent_steps,
    )
    selected_steps = []
    for index, agent_id in enumerate(selected_ids, start=1):
        step_id = f"custom_{index}"
        name = next(agent["name"] for agent in active_custom if agent["id"] == agent_id)
        dependencies = st.multiselect(
            f"依赖前置步骤 · {name}",
            [step["step_id"] for step in selected_steps],
            format_func=lambda selected_step_id: next(
                f"{prior['step_id']} · "
                f"{next(agent['name'] for agent in active_custom if agent['id'] == prior['agent_id'])}"
                for prior in selected_steps if prior["step_id"] == selected_step_id
            ),
            key=f"custom_dependencies_{agent_id}",
        )
        selected_steps.append({"step_id": step_id, "agent_id": agent_id,
                               "depends_on": dependencies})
    with st.form("plan_request"):
        request = st.text_area(
            "你的目标或请求",
            placeholder="明晚七点有 AI Agent 面试，请制定学习计划并安排任务",
            value=DEMO_REQUEST if service.demo_mode else "",
        )
        use_youtube = st.checkbox("本次使用 YouTube 公共视频资料（需先在 Agent 控制台启用两项只读操作）")
        youtube_query = st.text_input("YouTube 搜索词（仅选择 YouTube 时使用；最多 120 字）")
        submit = st.form_submit_button("生成计划", disabled=not service.model_ready)
    if submit:
        try:
            with st.status("Agent 正在协作规划", expanded=True) as status:
                key = uuid4().hex
                seen_events: set[str] = set()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        service.start_plan, request,
                        conversation_id=st.session_state.conversation_id,
                        idempotency_key=key,
                        custom_steps=selected_steps,
                        use_youtube=use_youtube,
                        youtube_query=youtube_query if use_youtube else None,
                    )
                    while not future.done():
                        _show_live_events(service, key, seen_events, status)
                        time.sleep(0.2)
                    _show_live_events(service, key, seen_events, status)
                    draft = future.result()
                st.session_state.selected_run_id = draft.run_id
                st.session_state.plan_run_select = draft.run_id
                status.update(label="规划草案已生成，等待你的确认", state="complete")
        except Exception as exc:
            show_error(exc)

    history = service.plan_history(st.session_state.conversation_id)
    st.subheader("最近三次请求")
    if history:
        st.dataframe([
            {"请求": row["request"], "答复摘要": row["summary"], "状态": zh(row["status"])}
            for row in history
        ], hide_index=True)
    else:
        st.caption("还没有规划记录。")

    all_plans = [row for row in service.list_runs() if row["kind"] == "planning"]
    if all_plans:
        run_ids = [row["id"] for row in all_plans]
        selected = st.session_state.get("selected_run_id", run_ids[0])
        if selected not in run_ids:
            selected = run_ids[0]
        index = run_ids.index(selected)
        selected = st.selectbox(
            "查看计划运行", run_ids, index=index,
            format_func=lambda item: next(
                f"{row['request_text'][:35]} · {zh(row['status'])}" for row in all_plans if row["id"] == item
            ),
            key="plan_run_select",
        )
        st.session_state.selected_run_id = selected
        run = service.get_run(selected)
        st.write(f"运行状态：**{zh(run['status'])}**")
        steps = service.list_run_steps(selected)
        if steps:
            st.dataframe([
                {"智能体": zh_role(step["agent"]), "步骤": zh_step(step["step_id"]),
                 "状态": zh(step["status"]), "依赖": "、".join(
                     zh_step(item) for item in json.loads(step["depends_on_json"])),
                 "错误": zh(step["error_code"])}
                for step in steps
            ], hide_index=True)
        if run["draft_json"]:
            draft = service.get_plan(selected)
            st.subheader("计划建议")
            for explanation in draft.explanations:
                st.write(f"• {zh_explanation(explanation)}")
            for task in draft.tasks:
                if task.resource_url:
                    st.link_button(
                        f"观看资料：{task.title} · {task.resource_channel} · "
                        f"{task.resource_duration_seconds // 60} 分钟 · {task.resource_source}",
                        task.resource_url,
                    )
            if draft.memory_ids:
                st.caption("依据记忆：" + ", ".join(draft.memory_ids))
            for conflict in draft.conflicts:
                st.warning(zh_issue(conflict))
            _recurrence_review(service, draft, run["status"])
            if run["status"] == "waiting_approval":
                _review_form(service, draft)
            else:
                st.dataframe([
                    {"任务": task.title, "领域": zh(task.domain),
                     "来源": zh_step(task.source_step_id), "优先级": zh(task.priority),
                     "预计分钟": task.estimated_minutes,
                     "开始": local_text(task.start_at.isoformat() if task.start_at else None, service.timezone),
                     "结束": local_text(task.end_at.isoformat() if task.end_at else None, service.timezone)}
                    for task in draft.tasks
                ], hide_index=True)

    render_daily_view(service)


def _recurrence_review(service: PersonalAIService, draft, run_status: str) -> None:
    suggestions = service.list_recurrence_suggestions(draft.run_id)
    if not suggestions:
        return
    st.subheader("重复规则建议")
    labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    for suggestion in suggestions:
        st.write(f"{suggestion['title']} · {suggestion['reason']}")
        if suggestion["confirmed"]:
            st.caption(f"已确认规则：{suggestion['rule_id']}")
            continue
        if run_status not in {"waiting_approval", "success"}:
            st.caption("该计划已结束，不能确认新的重复规则。")
            continue
        task = next(item for item in draft.tasks
                    if item.draft_item_id == suggestion["item_id"])
        zone = ZoneInfo(suggestion["timezone"])
        first_day = datetime.now(zone).date()
        if task.start_at:
            first_day = max(first_day, task.start_at.astimezone(zone).date() + timedelta(days=1))
        with st.form(f"confirm_recurrence_{draft.run_id}_{suggestion['item_id']}"):
            selected = st.multiselect(
                "确认每周三个星期", labels,
                default=[labels[index] for index in suggestion["weekdays"]],
            )
            start_day = st.date_input("重复规则起始日期", first_day)
            confirm = st.form_submit_button("确认并创建建议的重复规则")
        if confirm:
            try:
                service.confirm_recurrence_suggestion(
                    draft.run_id, draft.revision, suggestion["item_id"],
                    [labels.index(value) for value in selected], start_day,
                )
                st.rerun()
            except Exception as exc:
                show_error(exc)


def _show_live_events(service: PersonalAIService, key: str, seen: set[str], status) -> None:
    run = next(
        (row for row in service.list_runs() if row["idempotency_key"] == key), None
    )
    if run is None:
        return
    for event in service.list_trace(run["id"]):
        if event["id"] in seen:
            continue
        seen.add(event["id"])
        if event["event_type"] == "step_started":
            agent = json.loads(event["summary_json"])["agent"]
            status.update(label=f"{zh_role(agent)}正在执行", state="running")
            st.write(f"{zh_role(agent)}：运行中")
        elif event["event_type"] in {"step_succeeded", "step_failed", "tool_called", "tool_denied"}:
            summary = json.dumps(zh_data(json.loads(event["summary_json"])), ensure_ascii=False)
            st.write(f"{zh_step(event['step_id']) if event['step_id'] else '运行'}："
                     f"{zh(event['event_type'])} · {summary}")


def _review_form(service: PersonalAIService, draft) -> None:
    st.caption(f"草案版本：{draft.revision}。保存修改后版本会增加。")
    with st.form(f"edit_plan_{draft.run_id}_{draft.revision}"):
        updates: list[TaskDraft] = []
        for index, task in enumerate(draft.tasks):
            st.markdown(f"**任务 {index + 1} · {zh(task.domain)} · 来源 {zh_step(task.source_step_id)}**")
            title = st.text_input("标题", task.title, key=f"draft_title_{index}")
            minutes = st.number_input(
                "预计分钟", min_value=1, value=task.estimated_minutes,
                key=f"draft_minutes_{index}",
            )
            priority = st.selectbox(
                "优先级", ["low", "medium", "high"],
                index=["low", "medium", "high"].index(task.priority),
                format_func=zh,
                key=f"draft_priority_{index}",
            )
            start = st.text_input(
                "开始时间（ISO 8601，可留空）",
                local_text(task.start_at.isoformat() if task.start_at else None, service.timezone),
                key=f"draft_start_{index}",
            )
            end = st.text_input(
                "结束时间（ISO 8601，可留空）",
                local_text(task.end_at.isoformat() if task.end_at else None, service.timezone),
                key=f"draft_end_{index}",
            )
            updates.append(task.model_copy(update={
                "title": title, "estimated_minutes": minutes, "priority": priority,
                "start_at": start, "end_at": end,
            }))
        save = st.form_submit_button("保存计划修改")
    if save:
        try:
            checked = [
                TaskDraft.model_validate({
                    **task.model_dump(exclude={"start_at", "end_at"}),
                    "start_at": parse_datetime(task.start_at),
                    "end_at": parse_datetime(task.end_at),
                })
                for task in updates
            ]
            service.edit_plan(draft.run_id, draft.revision, checked)
            st.success("草案已更新；请检查冲突后确认。")
            st.rerun()
        except Exception as exc:
            show_error(exc)

    left, right = st.columns(2)
    with left:
        if st.button("确认并加入待办", key=f"approve_{draft.run_id}_{draft.revision}"):
            try:
                service.approve_plan(draft.run_id, draft.revision)
                st.success("任务已加入待办。")
                st.rerun()
            except Exception as exc:
                show_error(exc)
    with right:
        if st.button("拒绝计划", key=f"reject_{draft.run_id}_{draft.revision}"):
            try:
                service.reject_plan(draft.run_id, draft.revision)
                st.info("已拒绝计划。")
                st.rerun()
            except Exception as exc:
                show_error(exc)
