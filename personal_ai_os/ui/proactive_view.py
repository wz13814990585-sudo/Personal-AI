"""Review grounded suggestions and immutable strategy history on today's page."""

from __future__ import annotations

import streamlit as st

from personal_ai_os.display_zh import zh, zh_data, zh_policy
from personal_ai_os.services import PersonalAIService
from .common import show_error


_THRESHOLD_LABEL = {
    "overdue_task": "逾期识别延迟（天）",
    "habit_gap": "习惯差距触发次数",
    "daily_review": "复盘未完成触发项数",
}
_MINIMUM = {"overdue_task": 0, "habit_gap": 1, "daily_review": 1}
_MAXIMUM = {"overdue_task": 7, "habit_gap": 7, "daily_review": 20}


def render(service: PersonalAIService) -> None:
    st.subheader("主动建议与策略")
    st.caption("每日复盘后由本机后台任务进程自动扫描；也可手动扫描。只读取已批准记忆和真实任务、习惯、复盘记录。接受仅调整后续建议阈值，不会创建任务、改日程或写长期记忆。")
    if st.button("扫描主动建议"):
        try:
            service.scan_proactive_suggestions()
            st.rerun()
        except Exception as exc:
            show_error(exc)

    pending = service.list_proactive_suggestions("pending")
    if not pending:
        st.caption("暂无待审核主动建议。")
    else:
        selected_id = st.selectbox(
            "选择待审核主动建议", [item["id"] for item in pending],
            format_func=lambda item: next(
                row["title"] for row in pending if row["id"] == item
            ),
        )
        selected = next(item for item in pending if item["id"] == selected_id)
        st.write(selected["explanation"])
        st.caption(f"生成时间：{selected['generated_at_utc']} · 去重键：{selected['dedupe_key'][:12]} · "
                   f"来源 ID：{', '.join(selected['source_ids'])} · 基于策略 v{selected['strategy_version']}")
        with st.expander("查看可核查的来源与事实"):
            st.json(zh_data(selected["evidence"]))
        with st.form(f"edit_suggestion_{selected_id}_{selected['revision']}"):
            title = st.text_input("修改建议标题", selected["title"])
            explanation = st.text_area("修改建议说明", selected["explanation"])
            threshold = st.number_input(
                _THRESHOLD_LABEL[selected["kind"]],
                min_value=_MINIMUM[selected["kind"]], max_value=_MAXIMUM[selected["kind"]],
                value=selected["proposed_threshold"], step=1,
            )
            save = st.form_submit_button("保存建议修改")
        if save:
            try:
                service.edit_proactive_suggestion(
                    selected_id, selected["revision"], title, explanation, int(threshold),
                )
                st.rerun()
            except Exception as exc:
                show_error(exc)
        left, right = st.columns(2)
        with left:
            if st.button("接受建议并应用策略", key=f"accept_suggestion_{selected_id}"):
                try:
                    service.resolve_proactive_suggestion(selected_id, selected["revision"], True)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)
        with right:
            if st.button("拒绝主动建议", key=f"reject_suggestion_{selected_id}"):
                try:
                    service.resolve_proactive_suggestion(selected_id, selected["revision"], False)
                    st.rerun()
                except Exception as exc:
                    show_error(exc)

    history = service.list_proactive_suggestions()
    if history:
        st.dataframe([{
            "建议": item["title"], "状态": zh(item["status"]),
            "生成时间": item["generated_at_utc"],
            "来源 ID": ", ".join(item["source_ids"]),
            "接受后策略版本": item["accepted_strategy_version"] or "",
        } for item in history[:20]], hide_index=True)

    strategy = service.current_suggestion_strategy()
    st.write(f"当前建议策略：**版本 {strategy['version']}** · {zh_policy(strategy['policy'])}")
    versions = service.list_suggestion_strategy_versions()
    st.dataframe([{
        "版本": item["version"], "当前": "是" if item["active"] else "否",
        "策略阈值": zh_policy(item["policy"]),
        "上一版本": item["parent_version"] or "",
        "回退目标": item["rollback_target_version"] or "",
        "接受建议 ID": item["source_suggestion_id"] or "",
        "建立时间": item["created_at_utc"],
    } for item in versions], hide_index=True)
    previous = [item for item in versions if not item["active"]]
    if previous:
        target = st.selectbox(
            "选择要回退的策略版本", [item["version"] for item in previous],
            format_func=lambda version: next(
                f"版本 {item['version']} · {zh_policy(item['policy'])}"
                for item in previous if item["version"] == version
            ),
        )
        if st.button("回退建议策略"):
            try:
                service.rollback_suggestion_strategy(target, strategy["version"])
                st.rerun()
            except Exception as exc:
                show_error(exc)
