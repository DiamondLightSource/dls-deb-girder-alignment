import subprocess
import sys

from dls_deb_girder_alignment import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "dls_deb_girder_alignment", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__
