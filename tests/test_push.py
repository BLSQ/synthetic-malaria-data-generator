import pytest
import yaml

from malsim.config import ConfigError
from malsim.push import push
from malsim.run import generate
from conftest import TINY_CONFIG, FakeDhis2Client

N_VALUES = 6 * 36 * 4 * 2


@pytest.fixture
def out(tmp_path):
    generate(TINY_CONFIG, out_dir=tmp_path / "o")
    return tmp_path / "o"


def test_dry_run_by_default_in_chunks(out):
    client = FakeDhis2Client()
    report = push(out, client, chunk_size=500)
    assert len(client.posts) == -(-N_VALUES // 500)
    assert all(p["dry_run"] for p in client.posts)
    assert all(p["payload"]["dataSet"] == "DsMonthly01" for p in client.posts)
    assert report["values_sent"] == N_VALUES
    assert report["import_count"]["imported"] == N_VALUES
    assert (out / "push_report_dryrun.yaml").is_file() and not (out / "push_report.yaml").exists()


def test_commit(out):
    client = FakeDhis2Client()
    push(out, client, commit=True, chunk_size=10_000)
    assert [p["dry_run"] for p in client.posts] == [False]
    saved = yaml.safe_load((out / "push_report.yaml").read_text(encoding="utf-8"))
    assert saved["commit"] is True and saved["conflict_chunks"] == 0


def test_stops_after_conflicting_chunks(out):
    bad = {"httpStatus": "Conflict", "response": {"status": "WARNING", "conflicts": [{"object": "x", "value": "bad"}],
                                                  "importCount": {"imported": 0, "ignored": 500}}}
    client = FakeDhis2Client(import_responses=[bad, bad, bad])
    report = push(out, client, chunk_size=500, max_conflict_chunks=2)
    assert report["stopped_early"] and report["chunks"] == 2 and len(client.posts) == 2
    assert report["import_count"]["ignored"] == 1000
    assert report["conflicts"][0]["value"] == "bad"


def test_refuses_another_instance(out):
    resolved = yaml.safe_load((out / "dhis2_mapping.yaml").read_text(encoding="utf-8"))
    resolved["instance"] = "https://elsewhere.example.org/api"
    (out / "dhis2_mapping.yaml").write_text(yaml.safe_dump(resolved), encoding="utf-8")
    with pytest.raises(ConfigError, match="mapped against"):
        push(out, FakeDhis2Client())


def test_needs_generate_output(tmp_path):
    with pytest.raises(FileNotFoundError):
        push(tmp_path, FakeDhis2Client())
