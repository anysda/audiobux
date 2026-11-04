import dataclasses
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from audiobux import auth
from audiobux.config import Settings

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
def test_the_installer_passes_shellcheck():
    run = subprocess.run(["shellcheck", "install.sh"], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout


def test_the_example_settings_are_the_ones_compose_reads_and_the_readme_lists():
    compose = (ROOT / "compose.yaml").read_text()
    used = set(re.findall(r"\$\{(AUDIOBUX_\w+)", compose))
    given = set(re.findall(r"^(AUDIOBUX_\w+)=", (ROOT / ".env.example").read_text(), re.M))
    table = (ROOT / "README.md").read_text().partition("## Настройки")[2].partition("###")[0]
    assert used == given == set(re.findall(r"`(AUDIOBUX_\w+)`", table))


def test_a_setting_from_env_reaches_the_process_that_reads_it():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    # what the README tells the user to set
    told = set(re.findall(r"`AUDIOBUX_(\w+)", (ROOT / "README.md").read_text()))
    fields = {f.name for f in dataclasses.fields(Settings)}

    def read(paths) -> set[str]:
        return {n for p in paths for n in re.findall(r"settings\.(\w+)", p.read_text())} & fields

    web = ROOT / "audiobux" / "web.py"
    worker = [p for p in (ROOT / "audiobux").rglob("*.py") if p.name not in ("web.py", "config.py")]
    for service, names in (("web", read([web])), ("worker", read(worker))):
        passed = {k.removeprefix("AUDIOBUX_").lower() for k in services[service]["environment"]}
        missing = {n for n in names if n.upper() in told} - passed
        assert not missing, (service, missing)


def test_the_web_is_open_to_this_machine_only_unless_told_otherwise():
    web = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["web"]
    assert web["ports"] == ["${AUDIOBUX_BIND:-127.0.0.1}:${AUDIOBUX_PORT:-8080}:8080"]
    assert re.search(r"^AUDIOBUX_BIND=127\.0\.0\.1$", (ROOT / ".env.example").read_text(), re.M)


def test_the_card_works_round_the_clock_unless_told_otherwise():
    assert re.search(r"^AUDIOBUX_QUIET_HOURS=$", (ROOT / ".env.example").read_text(), re.M)


def test_the_time_zone_is_the_hosts():
    """Quiet hours run by the server clock: no setting of its own, the host's zone in both processes."""
    texts = [(ROOT / p).read_text() for p in ("compose.yaml", ".env.example", "README.md", "docs/architecture.md")]
    assert not [t for t in texts if "AUDIOBUX_TZ" in t]
    services = yaml.safe_load(texts[0])["services"]
    for service in ("worker", "web"):
        assert "/etc/localtime:/etc/localtime:ro" in services[service]["volumes"], service


def test_compose_runs_every_image_by_the_digest_install_writes():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for name in ("worker", "web", "cover"):
        pinned = f"ghcr.io/anysda/audiobux/{name}@${{AUDIOBUX_{name.upper()}_DIGEST:?"
        assert services[name]["image"].startswith(pinned), name


def test_the_worker_has_no_way_out_and_runs_on_what_fetch_put_in_place():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services, networks = compose["services"], compose["networks"]
    worker, fetch = services["worker"], services["fetch"]
    assert all(networks[n].get("internal") for n in worker["networks"])
    assert worker["environment"]["HF_HUB_OFFLINE"] == "1"
    # one run, with the image and the settings the models are chosen by of the worker
    assert (fetch["restart"], fetch["command"], fetch["image"]) == ("no", ["audiobux", "fetch"], worker["image"])
    assert fetch["environment"].items() <= worker["environment"].items()
    # out go only fetch, for the models, and cover, for the covers
    out = {s for s, v in services.items() if any(not networks[n].get("internal") for n in v["networks"])}
    assert out - {"web"} == {"fetch", "cover"}


def test_the_install_does_not_wait_for_the_models():
    """up -d waits for every dependency of what it starts: none on fetch, and no run of fetch in sight."""
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    assert not [s for s, v in services.items() if "fetch" in v.get("depends_on", {})]
    installer = (ROOT / "install.sh").read_text()
    assert re.findall(r"docker compose up\b.*", installer) == ["docker compose up -d ||"]
    assert 'say "Модели качаются в фоне, ход загрузки виден в интерфейсе."' in installer


def shell(tmp_path, name: str, call: str, tools: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`name` of install.sh called as `call`, with these tools in place of the real ones."""
    fn = re.search(rf"^{name}\(\) \{{$.*?^\}}$", (ROOT / "install.sh").read_text(), re.M | re.S)
    assert fn
    for tool, body in tools.items():
        (tmp_path / tool).write_text(f"#!/bin/sh\n{body}\n")
        (tmp_path / tool).chmod(0o755)
    stubs = "say() { :; }\nfail() { printf '%s\\n' \"$@\" >&2; exit 1; }\n"
    return subprocess.run(
        ["bash", "-c", stubs + fn[0] + "\n" + call],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("curl", "code", "out"),
    [
        ("echo https://github.com/anysda/audiobux/releases/tag/v0.6.0", 0, "v0.6.0\n"),
        ("echo https://github.com/anysda/audiobux/releases", 1, "ещё нет выпуска"),
        ("exit 6", 1, "не узнать последний выпуск"),
    ],
    ids=["latest", "none-yet", "no-network"],
)
def test_the_latest_release_is_the_one_github_leads_to(tmp_path, curl, code, out):
    run = shell(tmp_path, "latest_release", "latest_release", {"curl": curl})
    assert run.returncode == code, run.stderr
    assert out in (run.stdout if code == 0 else run.stderr)


def test_the_install_needs_no_git():
    """From an archive or a bare main alike: what is installed is told by GitHub, not by .git."""
    code = [line for line in (ROOT / "install.sh").read_text().splitlines() if not line.lstrip().startswith("#")]
    assert not [line for line in code if re.search(r"\bgit\b", line)]


GOOD = "repo@sha256:" + "a" * 64
OTHER = "repo@sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("there", "code", "said"),
    [(GOOD, 0, ""), (f"{OTHER} {GOOD}x", 1, "digest не совпал"), (None, 1, "не скачался")],
    ids=["the-built-one", "another-under-the-tag", "none"],
)
def test_an_image_of_the_release_is_taken_only_with_its_digest(tmp_path, there, code, said):
    """verify_image of install.sh, with a docker whose pull fails and whose image has these RepoDigests."""
    inspect = f'echo "{there}"' if there else "exit 1"
    docker = f'[ "$1" = pull ] && exit 1\n{inspect}'
    run = shell(tmp_path, "verify_image", "verify_image repo v1 sha256:" + "a" * 64, {"docker": docker})
    assert run.returncode == code, run.stderr
    assert said in run.stderr


@pytest.mark.parametrize(
    ("password", "code"), [("", 0), ("д" * 11, 1), ("д" * 12, 0), ("x" * 12, 0)], ids=["unset", "11", "12", "ascii"]
)
def test_a_short_password_stops_the_install_before_anything(tmp_path, password, code):
    """Counted in letters: a Cyrillic one is not taken for twice as long."""
    run = shell(tmp_path, "check_password", f"AUDIOBUX_PASSWORD='{password}' check_password", {})
    assert run.returncode == code, run.stderr
    assert ("короче 12 символов" in run.stderr) == bool(code)


def test_the_password_the_install_makes_is_long_enough():
    made = re.search(r"fresh=\$\(openssl rand -base64 (\d+) ", (ROOT / "install.sh").read_text())
    assert made and int(made[1]) * 4 // 3 >= auth.PASSWORD_MIN
