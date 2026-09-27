import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
def test_the_installer_passes_shellcheck():
    run = subprocess.run(["shellcheck", "install.sh"], cwd=ROOT, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout
