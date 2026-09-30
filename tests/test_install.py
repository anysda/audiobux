import dataclasses
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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


def test_the_card_works_round_the_clock_unless_told_otherwise():
    assert re.search(r"^AUDIOBUX_QUIET_HOURS=$", (ROOT / ".env.example").read_text(), re.M)
