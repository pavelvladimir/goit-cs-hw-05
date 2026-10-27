import csv
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.benchmark import Config, Trial, benchmark_words, run_benchmark, summarize


@pytest.mark.parametrize(("warmups", "relative_output"), [(0, False), (1, True)])
def test_benchmark_bundle_has_verified_samples_and_no_working_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    warmups: int,
    relative_output: bool,
) -> None:
    config = Config(
        small_files=3,
        small_bytes=17,
        large_files=1,
        large_mib=1,
        words=40,
        repeats=2,
        warmups=warmups,
    )
    monkeypatch.chdir(tmp_path)
    output_root = Path("results") if relative_output else tmp_path / "results"
    folder = run_benchmark(config, output_root)
    terminal_output = capsys.readouterr()
    assert terminal_output.out.splitlines()[-1] == f"Результати: {folder.name}"
    assert str(tmp_path) not in terminal_output.out + terminal_output.err
    report_text = (folder / "results.json").read_text()
    result = json.loads(report_text)
    location = Path(result["environment"]["filesystem_location"])
    assert not location.is_absolute()
    assert ".." not in location.parts
    assert (output_root / location).resolve() == folder.resolve()
    assert str(tmp_path) not in report_text
    assert len(result["trials"]) == 15 * (2 + warmups)
    assert sum(trial["warmup"] for trial in result["trials"]) == 15 * warmups
    assert len(result["summary"]) == 15
    assert all(row["samples"] == 2 for row in result["summary"])
    assert {p.name for p in folder.iterdir()} == {
        "results.json",
        "trials.csv",
        "comparison.png",
    }
    assert (folder / "comparison.png").read_bytes().startswith(b"\x89PNG")
    with (folder / "trials.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 15 * (2 + warmups)
    assert len(result["inputs"]["files_sha256"]["many_small"]) == 3
    assert result["config"]["seed"] == 42


def test_summary_excludes_warmups_and_uses_median() -> None:
    trials = [
        Trial(
            "sample",
            "method",
            workers,
            index,
            index == 0,
            value * (workers or 1),
            1,
            1024,
        )
        for workers in (0, 1, 4, 8, 16)
        for index, value in enumerate((1000.0, 1.0, 3.0, 2.0))
    ]
    rows = summarize(trials)
    assert rows[0]["median_seconds"] == 2.0
    assert rows[0]["min_seconds"] == 1.0
    assert rows[0]["max_seconds"] == 3.0
    assert rows[2]["speedup_vs_baseline"] == 0.25


def test_benchmark_rejects_incorrect_word_counts() -> None:
    import random

    with (
        patch("scripts.benchmark.map_reduce", return_value={}),
        pytest.raises(RuntimeError, match="Counter"),
    ):
        benchmark_words("one two one", Config(repeats=1, warmups=0), random.Random(42))


def test_invalid_benchmark_configuration_creates_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_benchmark(Config(small_files=0), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_benchmark_rejects_corrupted_copy_and_cleans_workdir(tmp_path: Path) -> None:
    from scripts import benchmark

    original = benchmark.sequential_copy

    def corrupt(source: Path, output: Path) -> None:
        original(source, output)
        next(output.rglob("*.bin")).write_bytes(b"corrupted")

    with patch.object(benchmark, "sequential_copy", corrupt):
        with pytest.raises(RuntimeError, match="SHA-256"):
            run_benchmark(
                Config(small_files=1, small_bytes=10, repeats=1, warmups=0), tmp_path
            )
    assert not list(tmp_path.rglob("data-*"))
    assert not list(tmp_path.rglob("results.json"))


def test_cli_accepts_zero_warmups(tmp_path: Path) -> None:
    from scripts import benchmark

    with (
        patch.object(benchmark, "configure_logging"),
        patch.object(benchmark, "run_benchmark", return_value=tmp_path) as run,
    ):
        assert benchmark.main(["--warmups", "0", "--output", str(tmp_path)]) == 0
    assert run.call_args.args[0].warmups == 0
