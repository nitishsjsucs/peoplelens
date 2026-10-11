"""Seeds are byte-identical to a fresh generation and to SHA256SUMS."""

from __future__ import annotations

from peoplelens.paths import SEEDS_DIR, SHA256SUMS_PATH
from peoplelens.synth import spec
from peoplelens.synth.generator import render, sha256sums


def test_generation_is_deterministic():
    assert render() == render()


def test_seed_files_match_fresh_generation():
    files = render()
    assert sorted(files) == sorted(spec.SEED_FILES)
    for name, data in files.items():
        assert (SEEDS_DIR / name).read_bytes() == data, f"{name} drifted; run `uv run pl data generate`"


def test_sha256sums_file_matches():
    assert SHA256SUMS_PATH.read_bytes() == sha256sums(render())


def test_csv_line_endings_survive_checkout():
    # Python's csv module writes \r\n; .gitattributes marks *.csv -text so git keeps them.
    data = (SEEDS_DIR / "raw_employees.csv").read_bytes()
    assert b"\r\n" in data
    assert data.count(b"\n") == data.count(b"\r\n")
