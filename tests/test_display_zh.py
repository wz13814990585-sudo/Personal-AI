"""Display translation must not alter external links or persisted protocol values."""

from personal_ai_os.display_zh import zh_explanation, zh_issue


def test_explanation_translation_preserves_external_url() -> None:
    original = (
        "learning: due_at 前完成 high priority 复习；"
        "资料 https://example.test/watch?priority=high&due_at=tomorrow"
    )
    displayed = zh_explanation(original)
    assert displayed.startswith("学习智能体：截止时间 前完成 高 优先级 复习")
    assert "https://example.test/watch?priority=high&due_at=tomorrow" in displayed


def test_compound_schedule_conflict_is_readable_without_changing_source() -> None:
    raw = "复习: no_legal_time:outside_availability,busy_overlap"
    assert zh_issue(raw) == "复习: 没有合法排程时间:不在可用时间内,与占用时间冲突"
