"""Real persisted run steps, delegation, tool calls, and failures."""

from __future__ import annotations

import json

import streamlit as st

from personal_ai_os.display_zh import zh, zh_data, zh_explanation, zh_role, zh_step
from personal_ai_os.services import PersonalAIService
from .common import show_error


def render(service: PersonalAIService) -> None:
    st.title("执行轨迹")
    usage = service.usage_summary()
    st.markdown("### DeepSeek 实际用量")
    st.write(f"请求 {usage['request_count']} 次 · 耗时 {usage['duration_ms']} ms · "
             f"已知输入/输出 token {usage['input_tokens']}/{usage['output_tokens']}")
    if usage["missing_token_records"]:
        st.caption(f"{usage['missing_token_records']} 次请求缺少 token 数据；已知合计不包含缺失值。")
    if usage["estimated_cost_usd"] is not None:
        st.caption(f"按用户填写单价估算：USD {usage['estimated_cost_usd']}；"
                   f"覆盖 {usage['estimated_request_count']}/{usage['request_count']} 次请求，非账单。")
    if usage["by_model"]:
        st.dataframe(usage["by_model"], hide_index=True)
    st.markdown("### 外部工具审计")
    external_events = service.list_external_audit()
    if external_events:
        st.dataframe([
            {"时间": event["created_at_utc"], "服务器": event["server_id"],
             "操作": zh(event["operation"]), "草案": event["proposal_id"],
             "事件": zh(event["event_type"]),
             "摘要": json.dumps(zh_data(json.loads(event["detail_json"])), ensure_ascii=False)}
            for event in external_events
        ], hide_index=True)
    else:
        st.caption("暂无外部工具事件。")
    st.markdown("### 自定义 Agent 运行")
    custom_runs = service.list_custom_agent_runs()
    if custom_runs:
        custom_run_id = st.selectbox(
            "选择自定义 Agent 运行", [run["id"] for run in custom_runs],
            format_func=lambda item: next(
                f"{zh(run['kind'])} · {zh(run['status'])} · {run['request_text'][:40]}"
                for run in custom_runs if run["id"] == item
            ),
        )
        selected = service.get_custom_agent_run(custom_run_id)
        st.write(f"运行编号：`{custom_run_id}` · 状态：**{zh(selected['status'])}**"
                 f" · 配置版本：{selected['agent_version'] or '提案'}")
        if selected["error_code"]:
            st.error(zh(selected["error_code"]))
        if selected["output"]:
            st.json(zh_data(selected["output"]))
        custom_usage = service.list_custom_agent_usage(custom_run_id)
        st.caption(f"本运行实际 DeepSeek 请求 {len(custom_usage)} 次")
        custom_events = service.list_custom_agent_trace(custom_run_id)
        if custom_events:
            st.dataframe([
                {"时间": event["created_at_utc"], "事件": zh(event["event_type"]),
                 "操作者": zh(event["actor"]),
                 "摘要": json.dumps(zh_data(json.loads(event["summary_json"])), ensure_ascii=False),
                 "耗时 ms": event["duration_ms"]}
                for event in custom_events
            ], hide_index=True)
    else:
        st.caption("暂无自定义 Agent 运行。")
    st.markdown("### 内置 Agent 运行")
    runs = service.list_runs()
    if not runs:
        st.info("暂无运行记录。")
        return
    run_id = st.selectbox(
        "选择运行", [run["id"] for run in runs],
        format_func=lambda item: next(
            f"{zh(run['kind'])} · {zh(run['status'])} · {run['request_text'][:40]}"
            for run in runs if run["id"] == item
        ),
    )
    run = service.get_run(run_id)
    st.write(f"运行编号：`{run_id}` · 类型：{zh(run['kind'])} · 状态：**{zh(run['status'])}**")
    if run.get("retry_of_run_id"):
        st.caption(f"重试来源运行：{run['retry_of_run_id']}")
    if run["status"] == "failed":
        label = "重试失败规划" if run["kind"] == "planning" else "重试反馈提取"
        if st.button(label):
            try:
                with st.status("正在重试；已验证且输入未变化的只读步骤可复用") as progress:
                    result = service.retry_run(run_id)
                    if run["kind"] == "planning":
                        progress.update(label=f"新草案 {result.run_id[:8]} 等待确认", state="complete")
                    else:
                        progress.update(label=f"新反馈运行生成 {len(result)} 条待审核候选", state="complete")
                st.rerun()
            except Exception as exc:
                show_error(exc)
    st.subheader("步骤与依赖")
    steps = service.list_run_steps(run_id)
    if steps:
        st.dataframe([
            {"步骤": zh_step(step["step_id"]), "智能体": zh_role(step["agent"]),
             "依赖": "、".join(zh_step(item) for item in json.loads(step["depends_on_json"])),
             "状态": zh(step["status"]), "错误": zh(step["error_code"])}
            for step in steps
        ], hide_index=True)
        for step in steps:
            if step["result_json"]:
                result = json.loads(step["result_json"])
                with st.expander(f"{zh_role(step['agent'])}的结果摘要"):
                    st.write(zh_explanation(result.get("reason", "")))
                    if result.get("custom_run_id"):
                        st.caption(f"自定义 Agent 运行编号：{result['custom_run_id']}")
                        st.json(zh_data(result.get("advice", {})))
                    if result.get("referenced_ids"):
                        st.write("引用编号：" + ", ".join(result["referenced_ids"]))
    st.subheader("逐步事件")
    measured = service.list_model_usage(run_id)
    if measured:
        st.caption(f"本运行实际 DeepSeek 请求 {len(measured)} 次；缺失 token 保留为空。")
    events = service.list_trace(run_id)
    if events:
        st.dataframe([
            {"时间": event["created_at_utc"],
             "步骤": zh_step(event["step_id"]) if event["step_id"] else "运行",
             "操作者": zh(event["actor"]), "事件": zh(event["event_type"]),
             "摘要": json.dumps(zh_data(json.loads(event["summary_json"])), ensure_ascii=False),
             "耗时 ms": str(event["duration_ms"]) if event["duration_ms"] is not None else ""}
            for event in events
        ], hide_index=True)
    else:
        st.caption("暂无事件。")
