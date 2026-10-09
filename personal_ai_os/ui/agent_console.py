"""Inspect built-in roles and manage user-defined Agent metadata."""

from __future__ import annotations

import streamlit as st

from personal_ai_os.contracts import CustomAgentCreate
from personal_ai_os.display_zh import zh, zh_data, zh_role
from personal_ai_os.services import PersonalAIService
from .common import show_error
from .external_view import render as render_external


def render(service: PersonalAIService) -> None:
    st.title("Agent 控制台")
    st.caption("查看协作角色、自定义助手与外部连接。角色权限受代码级上限约束。")
    roles = service.list_roles()
    custom = service.list_custom_agents()
    first, second = st.columns(2)
    first.metric("内置 Agent", len(roles))
    second.metric("已启用自定义 Agent", sum(agent["status"] == "active" for agent in custom))
    built_in_tab, custom_tab, external_tab = st.tabs(["内置 Agent", "自定义 Agent", "外部连接"])
    with built_in_tab:
        _render_builtin(service, roles)
    with custom_tab:
        _render_custom(service)
    with external_tab:
        if service.demo_mode:
            st.caption("演示模式已关闭外部账号和 MCP 接入；真实数据不会进入此演示。")
        else:
            render_external(service)


def _render_builtin(service: PersonalAIService, roles: list[dict]) -> None:
    runs = service.list_runs()
    for role in roles:
        recent_status = "未运行"
        for run in runs:
            matched = next(
                (step for step in service.list_run_steps(run["id"])
                 if step["agent"] == role["role"]), None
            )
            if matched:
                recent_status = zh(matched["status"])
                break
        with st.expander(f"{zh_role(role['role'])} · 版本 {role['version']} · {recent_status}"):
            st.write(role["description"])
            st.write("输入：" + role["input_contract"])
            st.write("输出：" + role["output_contract"])
            st.write("权限上限：" + "、".join(zh(item) for item in role["maximum_tools"]))
            st.write("当前工具：" + "、".join(zh(item) for item in role["tool_subset"]))
            with st.form(f"role_{role['role']}"):
                instructions = st.text_area("角色指令", role["instructions"])
                tools = st.multiselect(
                    "允许使用的工具", role["maximum_tools"],
                    default=role["tool_subset"],
                    format_func=zh,
                )
                save = st.form_submit_button("保存 Agent 配置")
            if save:
                try:
                    service.update_role(role["role"], instructions, set(tools))
                    st.rerun()
                except Exception as exc:
                    show_error(exc)

def _render_custom(service: PersonalAIService) -> None:
    st.subheader("自定义 Agent")
    st.caption("DeepSeek 生成的定义须先审核；采纳后默认暂停，启用后才能手动运行只读建议。")
    maximum = service.custom_agent_tool_maximum()
    with st.form("propose_custom_agent"):
        goal = st.text_area("希望新增 Agent 处理的目标")
        propose = st.form_submit_button("生成待审核 Agent 提案", disabled=not service.model_ready)
    if propose:
        try:
            with st.status("DeepSeek 正在生成待审提案") as progress:
                proposal = service.generate_custom_agent_proposal(goal)
                progress.update(label=f"提案 {proposal['id'][:8]} 待审核", state="complete")
            st.rerun()
        except Exception as exc:
            show_error(exc)

    for proposal in service.list_custom_agent_proposals("pending"):
        with st.expander(f"待审提案 · {proposal['name']} · v{proposal['revision']}"):
            st.write(proposal["explanation"])
            with st.form(f"review_custom_agent_{proposal['id']}"):
                reviewed_name = st.text_input(
                    "提案 Agent 名称", proposal["name"], key=f"proposal_name_{proposal['id']}"
                )
                reviewed_description = st.text_input(
                    "提案 Agent 描述", proposal["description"],
                    key=f"proposal_description_{proposal['id']}",
                )
                reviewed_instructions = st.text_area(
                    "提案 Agent 指令", proposal["instructions"],
                    key=f"proposal_instructions_{proposal['id']}",
                )
                reviewed_tools = st.multiselect(
                    "提案只读工具", maximum, default=proposal["suggested_tools"],
                    key=f"proposal_tools_{proposal['id']}",
                    format_func=zh,
                )
                accept = st.form_submit_button("审核并保存为暂停 Agent")
            if accept:
                try:
                    service.accept_custom_agent_proposal(
                        proposal["id"], proposal["revision"], CustomAgentCreate(
                            name=reviewed_name, description=reviewed_description,
                            instructions=reviewed_instructions, tool_subset=set(reviewed_tools),
                        ),
                    )
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            if st.button("拒绝 Agent 提案", key=f"reject_proposal_{proposal['id']}"):
                try:
                    service.reject_custom_agent_proposal(proposal["id"], proposal["revision"])
                    st.rerun()
                except Exception as exc:
                    show_error(exc)

    with st.form("create_custom_agent"):
        name = st.text_input("新 Agent 名称")
        description = st.text_input("新 Agent 描述")
        instructions = st.text_area("新 Agent 指令")
        tools = st.multiselect("新 Agent 只读工具", maximum, format_func=zh)
        create = st.form_submit_button("创建自定义 Agent")
    if create:
        try:
            service.create_custom_agent(CustomAgentCreate(
                name=name, description=description, instructions=instructions,
                tool_subset=set(tools),
            ))
            st.rerun()
        except Exception as exc:
            show_error(exc)

    for agent in service.list_custom_agents():
        with st.expander(f"{agent['name']} · 版本 {agent['version']} · {zh(agent['status'])}"):
            with st.form(f"edit_custom_agent_{agent['id']}"):
                edited_name = st.text_input(
                    "自定义 Agent 名称", agent["name"], key=f"name_{agent['id']}"
                )
                edited_description = st.text_input(
                    "自定义 Agent 描述", agent["description"], key=f"description_{agent['id']}"
                )
                edited_instructions = st.text_area(
                    "自定义 Agent 指令", agent["instructions"], key=f"instructions_{agent['id']}"
                )
                edited_tools = st.multiselect(
                    "自定义 Agent 只读工具", maximum, default=agent["tool_subset"],
                    key=f"tools_{agent['id']}",
                    format_func=zh,
                )
                save_custom = st.form_submit_button("保存自定义 Agent")
            if save_custom:
                try:
                    service.update_custom_agent(
                        agent["id"], agent["version"], CustomAgentCreate(
                            name=edited_name, description=edited_description,
                            instructions=edited_instructions, tool_subset=set(edited_tools),
                        ),
                    )
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            new_status = "paused" if agent["status"] == "active" else "active"
            if st.button(
                "暂停自定义 Agent" if new_status == "paused" else "启用自定义 Agent",
                key=f"status_{agent['id']}",
            ):
                try:
                    service.set_custom_agent_status(agent["id"], agent["version"], new_status)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
            if agent["status"] == "active":
                with st.form(f"run_custom_agent_{agent['id']}"):
                    request = st.text_area(
                        "给自定义 Agent 的只读请求", key=f"run_request_{agent['id']}"
                    )
                    start = st.form_submit_button(
                        "手动运行只读 Agent", disabled=not service.model_ready
                    )
                if start:
                    try:
                        with st.status("自定义 Agent 正在只读分析") as progress:
                            run = service.run_custom_agent(agent["id"], request)
                            progress.update(label="只读建议已生成", state="complete")
                        st.json(zh_data(run["output"]))
                    except Exception as exc:
                        show_error(exc)
            recent = next(
                (run for run in service.list_custom_agent_runs()
                 if run["agent_id"] == agent["id"]), None
            )
            if recent:
                st.caption(f"最近手动运行：{zh(recent['status'])} · {recent['id']}")
