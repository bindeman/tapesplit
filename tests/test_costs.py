from pathlib import Path

from tapesplit.costs import (
    estimate_api_cost_usd,
    estimate_llm_cost_usd,
    estimate_project_twelvelabs_index_cost,
)


def test_estimate_api_cost_uses_duration_rate():
    assert (
        estimate_api_cost_usd(
            provider="twelvelabs",
            service="index",
            units={"duration_min": 10},
        )
        == 0.42
    )


def test_estimate_llm_cost_accounts_for_cached_tokens(tmp_path, monkeypatch):
    rates = tmp_path / "cost_rates.json"
    rates.write_text(
        """
{
  "azure_openai": {
    "test-deployment": {
      "input_per_1m": 10,
      "cached_input_per_1m": 1,
      "output_per_1m": 20
    }
  }
}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert estimate_llm_cost_usd(
        provider="azure_openai",
        deployment="test-deployment",
        input_tokens=1_000_000,
        cached_input_tokens=250_000,
        output_tokens=100_000,
    ) == 9.75


def test_estimate_project_twelvelabs_index_cost(tmp_path):
    (tmp_path / "tapes.jsonl").write_text(
        '{"id":"video_1","filename":"a.mp4","probe":{"duration_s":120}}\n',
        encoding="utf-8",
    )

    estimate = estimate_project_twelvelabs_index_cost(tmp_path)

    assert estimate["video_count"] == 1
    assert estimate["duration_min"] == 2
    assert estimate["estimated_cost_usd"] == 0.084


def test_estimate_llm_cost_handles_null_cached_rate(tmp_path, monkeypatch):
    import json

    rates = {
        "azure_openai": {
            "gpt-4o-transcribe-diarize": {
                "input_per_1m": 2.5,
                "output_per_1m": 10.0,
                "cached_input_per_1m": None,
            }
        }
    }
    path = tmp_path / "cost_rates.json"
    path.write_text(json.dumps(rates), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cost = estimate_llm_cost_usd(
        provider="azure_openai",
        deployment="gpt-4o-transcribe-diarize",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cached_input_tokens=0,
    )

    assert cost == 3.5
