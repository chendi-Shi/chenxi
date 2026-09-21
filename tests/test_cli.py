import json

from research_agent.cli import date_bound, main
from research_agent.config import Settings
from research_agent.evaluation import evaluate
from research_agent.providers import RulesProvider


def test_cli_end_to_end(workspace, capsys):
    root = ["--data-dir", str(workspace / "db")]
    assert main(root + ["init"]) == 0
    capsys.readouterr()
    sample = workspace / "sample.txt"
    sample.write_text("营收12亿元，未经审计。", encoding="utf-8")
    assert main(root + ["ingest", str(sample)]) == 0
    capsys.readouterr()
    assert main(root + ["run", "--out", str(workspace / "exports")]) == 0
    result = json.loads(capsys.readouterr().out)
    run_id = result["run_id"]
    assert main(root + ["status", run_id]) == 0
    capsys.readouterr()
    assert main(root + ["review", run_id, "--reviewer", "test", "--status", "accepted"]) == 0
    capsys.readouterr()
    assert main(root + ["resume", run_id, "--out", str(workspace / "exports")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_cli_invalid_config_is_redacted(workspace, capsys):
    config = workspace / "invalid.toml"
    config.write_text('base_url="https://user:secret@example.com"', encoding="utf-8")
    assert main(["--config", str(config), "init"]) == 1
    output = capsys.readouterr().err
    assert "secret" not in output


async def test_gold_metrics(workspace):
    dataset = workspace / "gold.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "id": "one",
                "body": "营收12亿元。",
                "gold": [{"category": "公司业绩", "quote": "营收12亿元。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = await evaluate(dataset, Settings(), RulesProvider())
    assert result["exact_gold_recall"] == 1
    assert result["exact_gold_precision"] == 1


def test_date_bound_uses_requested_timezone():
    assert date_bound("2026-09-21", "Asia/Shanghai") == "2026-09-20T16:00:00+00:00"
