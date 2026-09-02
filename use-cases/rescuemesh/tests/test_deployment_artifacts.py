from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_docker_judge_image_runs_non_root_with_external_ledger():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "USER rescuemesh" in dockerfile
    assert "RESCUEMESH_LEDGER_PATH=/data/ledger.json" in dockerfile
    assert "EXPOSE 8000" in dockerfile


def test_compose_persists_command_center_ledger():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "8000:8000" in compose
    assert "rescuemesh-data:/data" in compose
