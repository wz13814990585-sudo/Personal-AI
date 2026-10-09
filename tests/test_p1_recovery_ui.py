"""Existing pages expose retry, measured usage and secret-free local downloads."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

from personal_ai_os.storage import Repository


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def field(items, label):
    return next(item for item in items if item.label == label)


def test_usage_prices_exports_and_retry_entry_stay_in_five_pages(tmp_path, monkeypatch):
    path = tmp_path / "recovery_ui.sqlite3"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    repo = Repository(path)
    repo.initialize()
    run = repo.create_run("失败的规划")
    repo.update_run_status(run["id"], "failed")
    at = AppTest.from_file(APP).run(timeout=30)
    at.switch_page("pages/04_trace.py").run(timeout=30)
    assert not at.exception
    assert any(item.label == "重试失败规划" for item in at.button)
    assert any("DeepSeek 实际用量" in item.value for item in at.markdown)
    at.switch_page("pages/05_memory.py").run(timeout=30)
    assert not at.exception
    field(at.text_input, "每百万输入 token 的美元单价").set_value("1.25")
    field(at.text_input, "每百万输出 token 的美元单价").set_value("2.5")
    field(at.button, "保存估算单价").click().run(timeout=30)
    assert repo.get_setting("model_prices")["deepseek-flash"] == {
        "input_per_million": "1.25", "output_per_million": "2.5"
    }
    assert not at.exception
    assert any(item.label == "导出本机数据 JSON" for item in at.get("download_button"))
    assert any(item.label == "导出任务 CSV" for item in at.get("download_button"))
