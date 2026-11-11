import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY

import pytest
import shellingham

from pdm.cli.commands.venv import backends
from pdm.cli.commands.venv.utils import (
    ACTIVE,
    MANUAL,
    ORPHANED,
    UNCONFIRMED,
    VENV_OWNER_FILE,
    classify_central_venv,
    get_venv_parent,
    get_venv_prefix,
    reconcile_central_venvs,
)
from pdm.models.venv import get_venv_python


@pytest.fixture(params=[True, False])
def with_pip(request):
    return request.param


@pytest.fixture()
def fake_create(monkeypatch):
    def fake_create(self, location, *args, prompt=None):
        bin_dir = "Scripts" if sys.platform == "win32" else "bin"
        suffix = ".exe" if sys.platform == "win32" else ""
        (location / bin_dir).mkdir(parents=True)
        (location / bin_dir / f"python{suffix}").touch()

    monkeypatch.setattr(backends.VirtualenvBackend, "perform_create", fake_create)
    monkeypatch.setattr(backends.VenvBackend, "perform_create", fake_create)
    monkeypatch.setattr(backends.CondaBackend, "perform_create", fake_create)


def make_central_venv(venv_parent: Path, name: str, owner: Path | str | None, *, corrupt: bool = False) -> Path:
    """Create a minimal venv-like entry under the central ``venv.location``.

    ``owner=None`` writes no ownership marker (manual/legacy entry); pass
    ``corrupt=True`` to write an unreadable marker.
    """
    path = venv_parent / name
    python = get_venv_python(path)
    python.parent.mkdir(parents=True)
    python.touch()
    if owner is not None:
        payload = {
            "schema": 1,
            "project": str(owner),
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        (path / VENV_OWNER_FILE).write_text(json.dumps(payload), encoding="utf-8")
    elif corrupt:
        (path / VENV_OWNER_FILE).write_text("{not valid json", encoding="utf-8")
    return path


def test_core_registration_does_not_import_runtime_helpers():
    code = """
import sys
from pdm.core import Core

Core()
lazy_modules = {
    "pdm.cli.commands.venv.backends",
    "pdm.cli.commands.venv.utils",
    "pbs_installer",
    "shellingham",
}
loaded = sorted(lazy_modules.intersection(sys.modules))
if loaded:
    print("\\n".join(loaded))
    raise SystemExit(1)
"""
    env = os.environ.copy()
    src = Path(__file__).resolve().parents[2] / "src"
    pythonpath = [str(src)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, text=True)

    assert result.returncode == 0, result.stdout or result.stderr


@pytest.mark.usefixtures("fake_create")
def test_venv_create(pdm, project):
    project._saved_python = None
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    venv_path = re.match(r"Virtualenv (.+) is created successfully", result.output).group(1)
    assert os.path.exists(venv_path)
    assert not project._saved_python


@pytest.mark.usefixtures("fake_create")
def test_venv_create_in_project(pdm, project):
    project.project_config["venv.in_project"] = True
    pdm(["venv", "create"], obj=project, strict=True)
    venv_path = project.root / ".venv"
    assert venv_path.exists()
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 1
    assert "is not empty" in result.stderr


@pytest.mark.usefixtures("fake_create")
def test_venv_create_other_location(pdm, project):
    pdm(["venv", "-p", project.root.as_posix(), "create"], strict=True)
    venv_path = project.root / ".venv"
    assert venv_path.exists()
    result = pdm(["venv", "-p", project.root.as_posix(), "create"])
    assert result.exit_code == 1
    assert "is not empty" in result.stderr


@pytest.mark.usefixtures("fake_create")
def test_venv_show_path(pdm, project):
    project.project_config["venv.in_project"] = True
    pdm(["venv", "create"], obj=project, strict=True)
    pdm(["venv", "create", "--name", "test"], obj=project, strict=True)
    result = pdm(["venv", "--path", "in-project"], obj=project, strict=True)
    assert result.output.strip() == str(project.root / ".venv")
    result = pdm(["venv", "--path", "test"], obj=project)
    assert result.exit_code == 0
    result = pdm(["venv", "--path", "foo"], obj=project)
    assert result.exit_code == 1


@pytest.mark.usefixtures("fake_create")
def test_venv_list(pdm, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    venv_path = re.match(r"Virtualenv (.+) is created successfully", result.output).group(1)

    result = pdm(["venv", "list"], obj=project)
    assert result.exit_code == 0, result.stderr
    assert venv_path in result.output


@pytest.mark.usefixtures("fake_create")
def test_venv_list_with_tilde_location(pdm, project, monkeypatch, tmp_path):
    home = tmp_path / "fake-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    project.global_config["venv.location"] = "~/my-venvs"
    project.project_config["venv.in_project"] = False

    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    venv_path = re.match(r"Virtualenv (.+) is created successfully", result.output).group(1)
    assert Path(venv_path).is_relative_to(home / "my-venvs")

    result = pdm(["venv", "list"], obj=project)
    assert result.exit_code == 0, result.stderr
    assert venv_path in result.output

    key = os.path.basename(venv_path)[len(get_venv_prefix(project)) :]
    result = pdm(["venv", "--path", key], obj=project)
    assert result.exit_code == 0, result.stderr
    assert result.output.strip() == venv_path


@pytest.mark.parametrize("dirname", ["my[project]", "downloads[1]"])
def test_iter_venvs_with_glob_chars_in_project_path(project, tmp_path, dirname):
    """A project directory name may legally contain glob metacharacters.

    ``venv create`` names the venv with the literal directory name, so the
    lookup must match that same literal name rather than treating it as a
    pattern.
    """
    from pdm.cli.commands.venv.utils import get_venv_parent, iter_venvs

    project.root = tmp_path / dirname
    project.root.mkdir()

    venv_parent = get_venv_parent(project)
    venv_parent.mkdir(parents=True, exist_ok=True)
    venv_dir = venv_parent / f"{get_venv_prefix(project)}3.13"
    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    bin_dir.mkdir(parents=True)
    bin_dir.joinpath("python.exe" if sys.platform == "win32" else "python").touch()

    assert {ident: venv.root for ident, venv in iter_venvs(project)} == {"3.13": venv_dir}


@pytest.mark.usefixtures("fake_create")
def test_venv_remove(pdm, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    venv_path = re.match(r"Virtualenv (.+) is created successfully", result.output).group(1)
    key = os.path.basename(venv_path)[len(get_venv_prefix(project)) :]

    result = pdm(["venv", "remove", "non-exist"], obj=project)
    assert result.exit_code != 0

    result = pdm(["venv", "remove", "-y", key], obj=project)
    assert result.exit_code == 0, result.stderr

    assert not os.path.exists(venv_path)


@pytest.mark.usefixtures("fake_create")
def test_venv_recreate(pdm, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr

    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code != 0

    result = pdm(["venv", "create", "-f"], obj=project)
    assert result.exit_code == 0, result.stderr


@pytest.mark.usefixtures("venv_backends")
def test_venv_activate(pdm, mocker, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    venv_path = re.match(r"Virtualenv (.+) is created successfully", result.output).group(1)
    key = os.path.basename(venv_path)[len(get_venv_prefix(project)) :]

    mocker.patch("shellingham.detect_shell", return_value=("bash", None))
    result = pdm(["venv", "activate", key], obj=project)
    assert result.exit_code == 0, result.stderr
    backend = project.config["venv.backend"]

    if backend == "conda":
        assert result.output.startswith("conda activate")
    else:
        assert result.output.strip("'\"\n").endswith("activate")
        if platform.system() == "Windows":
            assert not result.output.startswith("source")
            assert not result.output.startswith("'")
        else:
            assert result.output.startswith("source")


@pytest.mark.usefixtures("venv_backends")
@pytest.mark.skipif(platform.system() == "Windows", reason="UNIX only")
def test_venv_activate_tcsh(pdm, mocker, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    venv_path = re.match(r"Virtualenv (.+) is created successfully", result.output).group(1)
    key = os.path.basename(venv_path)[len(get_venv_prefix(project)) :]

    mocker.patch("shellingham.detect_shell", return_value=("tcsh", None))
    result = pdm(["venv", "activate", key], obj=project)
    assert result.output.startswith("source") and result.output.strip("'\"\n").endswith("activate.csh")


@pytest.mark.usefixtures("venv_backends")
def test_venv_activate_custom_prompt(pdm, mocker, project):
    project.project_config["venv.in_project"] = False
    creator = mocker.patch("pdm.cli.commands.venv.backends.Backend.create")
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    creator.assert_called_once_with(
        None, [], False, False, prompt=project.project_config["venv.prompt"], with_pip=False
    )


def test_venv_activate_project_without_python(pdm, project):
    project._saved_python = None
    result = pdm(["venv", "activate"], obj=project)
    assert result.exit_code != 0
    assert "The project doesn't have a saved python.path" in result.stderr


@pytest.mark.usefixtures("fake_create")
def test_venv_activate_error(pdm, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project, strict=True)

    result = pdm(["venv", "activate", "foo"], obj=project)
    assert result.exit_code != 0
    assert "No virtualenv with key" in result.stderr

    project._saved_python = os.path.abspath("fake/bin/python")
    result = pdm(["venv", "activate"], obj=project)
    assert result.exit_code != 0, result.output + result.stderr
    assert "Can't activate a non-venv Python" in result.stderr


@pytest.mark.usefixtures("venv_backends")
def test_venv_activate_no_shell(pdm, mocker, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project)
    assert result.exit_code == 0, result.stderr
    venv_path = re.match(r"Virtualenv (.+) is created successfully", result.output).group(1)
    key = os.path.basename(venv_path)[len(get_venv_prefix(project)) :]

    mocker.patch("shellingham.detect_shell", side_effect=shellingham.ShellDetectionFailure())
    result = pdm(["venv", "activate", key], obj=project)
    assert result.exit_code == 0, result.stderr
    backend = project.config["venv.backend"]

    if backend == "conda":
        assert result.output.startswith("conda activate")
    else:
        assert result.output.strip("'\"\n").endswith("activate")
        if platform.system() == "Windows":
            assert not result.output.startswith("source")
            assert not result.output.startswith("'")
        else:
            assert result.output.startswith("source")


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_venv_activate_powershell_quotes_script_path(mocker, tmp_path, shell):
    from pdm.cli.commands.venv.activate import ActivateCommand

    scripts = tmp_path / "my project's env" / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    (scripts / "Activate.ps1").touch()
    venv = mocker.Mock(interpreter=scripts / "python")
    mocker.patch("shellingham.detect_shell", return_value=(shell, None))

    escaped = str(scripts / "Activate.ps1").replace("'", "''")
    assert ActivateCommand().get_activate_command(venv) == f". '{escaped}'"


@pytest.mark.usefixtures("fake_create")
@pytest.mark.parametrize("keep_pypackages", [True, False])
def test_venv_auto_create(pdm, mocker, project, keep_pypackages):
    creator = mocker.patch("pdm.cli.commands.venv.backends.Backend.create")
    project._saved_python = None
    if keep_pypackages:
        project.root.joinpath("__pypackages__").mkdir(exist_ok=True)
    else:
        shutil.rmtree(project.root / "__pypackages__", ignore_errors=True)
    project.project_config["python.use_venv"] = True
    pdm(["install", "--no-self"], obj=project)
    if keep_pypackages:
        creator.assert_not_called()
    else:
        creator.assert_called_once()


@pytest.mark.usefixtures("fake_create")
def test_venv_purge_empty(pdm, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "purge"], obj=project)
    assert result.exit_code == 0, result.stderr
    assert "No virtualenvs to purge" in result.output


@pytest.mark.usefixtures("fake_create")
def test_central_venv_creation_writes_owner_marker(pdm, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project, strict=True)
    venv_path = Path(re.match(r"Virtualenv (.+) is created successfully", result.output).group(1))
    marker = venv_path / VENV_OWNER_FILE
    assert marker.is_file()
    assert json.loads(marker.read_text())["project"] == project.root.as_posix()


@pytest.mark.usefixtures("fake_create")
def test_in_project_venv_creation_skips_owner_marker(pdm, project):
    project.project_config["venv.in_project"] = True
    pdm(["venv", "create"], obj=project, strict=True)
    assert not (project.root / ".venv" / VENV_OWNER_FILE).exists()


def test_classify_central_venv_statuses(project, tmp_path):
    venv_parent = get_venv_parent(project)
    venv_parent.mkdir(parents=True)
    live_owner = tmp_path / "live-project"
    live_owner.mkdir()
    gone_owner = tmp_path / "gone-project"

    active = make_central_venv(venv_parent, "live-project-HASH1234-3.12", live_owner)
    orphan = make_central_venv(venv_parent, "gone-project-HASH5678-3.11", gone_owner)
    legacy = make_central_venv(venv_parent, "oldproject-Ab_CD123-3.10", None)
    manual = make_central_venv(venv_parent, "my-manual-env", None)
    corrupt = make_central_venv(venv_parent, "broken-project-HASH9012-3.9", None, corrupt=True)
    linked = venv_parent / "linked-env"
    linked.symlink_to(active, target_is_directory=True)

    assert classify_central_venv(active).status == ACTIVE
    assert classify_central_venv(orphan).status == ORPHANED
    assert classify_central_venv(legacy).status == UNCONFIRMED
    assert classify_central_venv(manual).status == MANUAL
    assert classify_central_venv(corrupt).status == UNCONFIRMED
    assert classify_central_venv(linked).status == MANUAL

    records = {record.name: record for record in reconcile_central_venvs(project)}
    assert set(records) == {active.name, orphan.name, legacy.name, manual.name, corrupt.name, linked.name}
    assert records[orphan.name].owner == gone_owner
    # Reconciliation order is deterministic, so interrupted runs converge.
    assert [record.name for record in reconcile_central_venvs(project)] == sorted(records)


@pytest.mark.usefixtures("fake_create")
def test_venv_purge_keeps_active_unconfirmed_and_manual(pdm, project, tmp_path):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project, strict=True)
    live_venv = Path(re.match(r"Virtualenv (.+) is created successfully", result.output).group(1))

    venv_parent = get_venv_parent(project)
    orphan = make_central_venv(venv_parent, "gone-project-HASH5678-3.11", tmp_path / "gone-project")
    legacy = make_central_venv(venv_parent, "oldproject-Ab_CD123-3.10", None)
    manual = make_central_venv(venv_parent, "my-manual-env", None)

    result = pdm(["venv", "purge", "-f"], obj=project, strict=True)
    assert not orphan.exists(), "confirmed orphan must be purged"
    assert live_venv.exists(), "active venv of an existing project must be kept"
    assert legacy.exists(), "unconfirmed legacy PDM venv must be kept"
    assert manual.exists(), "manually created directory must be kept"
    assert "Orphaned virtualenvs" in result.output
    assert "Active virtualenvs" in result.output

    # Re-running after the first purge is a no-op and yields the same tree.
    remaining_before = sorted(p.name for p in venv_parent.iterdir())
    result = pdm(["venv", "purge", "-f"], obj=project, strict=True)
    assert "No orphaned virtualenvs found" in result.output
    assert sorted(p.name for p in venv_parent.iterdir()) == remaining_before


@pytest.mark.usefixtures("fake_create")
def test_venv_purge_is_idempotent_after_interruption(pdm, project, tmp_path):
    venv_parent = get_venv_parent(project)
    orphans = [
        make_central_venv(venv_parent, f"gone-project-HASH000{i}-3.11", tmp_path / f"gone-{i}") for i in range(3)
    ]
    pdm(["venv", "purge", "-f"], obj=project, strict=True)
    assert all(not path.exists() for path in orphans)

    # A second run must not error nor touch anything else.
    result = pdm(["venv", "purge", "-f"], obj=project, strict=True)
    assert result.exit_code == 0
    assert all(not path.exists() for path in orphans)


@pytest.mark.usefixtures("fake_create")
@pytest.mark.parametrize("confirmed, removed", [(True, True), (False, False)])
def test_venv_purge_asks_for_confirmation(pdm, mocker, project, tmp_path, confirmed, removed):
    venv_parent = get_venv_parent(project)
    orphan = make_central_venv(venv_parent, "gone-project-HASH5678-3.11", tmp_path / "gone-project")
    mock_confirm = mocker.patch("pdm.termui.confirm", return_value=confirmed)
    result = pdm(["venv", "purge"], obj=project)
    assert result.exit_code == 0, result.stderr
    mock_confirm.assert_called_once()
    assert orphan.exists() != removed


@pytest.mark.usefixtures("fake_create")
def test_venv_purge_clears_saved_python_of_removed_venv(pdm, project, tmp_path):
    venv_parent = get_venv_parent(project)
    orphan = make_central_venv(venv_parent, "gone-project-HASH5678-3.11", tmp_path / "gone-project")
    project._saved_python = get_venv_python(orphan).as_posix()

    pdm(["venv", "purge", "-f"], obj=project, strict=True)
    assert not orphan.exists()
    assert project._saved_python is None, "stale .pdm-python record must not outlive the venv"


@pytest.mark.usefixtures("fake_create")
def test_venv_purge_keeps_saved_python_of_kept_venv(pdm, project):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project, strict=True)
    live_venv = Path(re.match(r"Virtualenv (.+) is created successfully", result.output).group(1))
    saved = get_venv_python(live_venv).as_posix()
    project._saved_python = saved

    pdm(["venv", "purge", "-f"], obj=project, strict=True)
    assert live_venv.exists()
    assert project._saved_python == saved


interactive_choices = [("none", False), ("0", True), ("all", True)]


@pytest.mark.usefixtures("fake_create")
@pytest.mark.parametrize("choice, removed", interactive_choices)
def test_venv_purge_interactive(pdm, choice, removed, project, tmp_path):
    venv_parent = get_venv_parent(project)
    orphan = make_central_venv(venv_parent, "gone-project-HASH5678-3.11", tmp_path / "gone-project")
    result = pdm(["venv", "purge", "-i"], input=choice, obj=project)
    assert result.exit_code == 0, result.stderr
    assert orphan.exists() != removed


@pytest.mark.usefixtures("fake_create")
def test_venv_purge_all_restores_blanket_behavior(pdm, project, tmp_path):
    project.project_config["venv.in_project"] = False
    result = pdm(["venv", "create"], obj=project, strict=True)
    live_venv = Path(re.match(r"Virtualenv (.+) is created successfully", result.output).group(1))
    venv_parent = get_venv_parent(project)
    manual = make_central_venv(venv_parent, "my-manual-env", None)
    linked = venv_parent / "linked-env"
    linked.symlink_to(live_venv, target_is_directory=True)

    pdm(["venv", "purge", "--all", "-f"], obj=project, strict=True)
    assert not live_venv.exists()
    assert not manual.exists()
    assert not linked.is_symlink()


def test_virtualenv_backend_create(project, mocker, with_pip):
    backend = backends.VirtualenvBackend(project, None)
    assert backend.ident

    def create_location(cmd, **kwargs):
        Path(cmd[3]).mkdir(parents=True)

    mock_call = mocker.patch("subprocess.check_call", side_effect=create_location)
    location = backend.create(with_pip=with_pip)
    pip_args = [] if with_pip else ["--no-pip", "--no-setuptools", "--no-wheel"]
    mock_call.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "virtualenv",
            str(location),
            "-p",
            str(backend._resolved_interpreter.executable),
            *pip_args,
        ],
        stdout=ANY,
    )
    marker = location / VENV_OWNER_FILE
    assert marker.is_file()
    assert json.loads(marker.read_text())["project"] == project.root.as_posix()


def test_venv_backend_create(project, mocker, with_pip):
    backend = backends.VenvBackend(project, None)
    assert backend.ident

    def create_location(cmd, **kwargs):
        Path(cmd[3]).mkdir(parents=True)

    mock_call = mocker.patch("subprocess.check_call", side_effect=create_location)
    location = backend.create(with_pip=with_pip)
    pip_args = [] if with_pip else ["--without-pip"]
    mock_call.assert_called_once_with(
        [
            str(backend._resolved_interpreter.executable),
            "-m",
            "venv",
            str(location),
            *pip_args,
        ],
        stdout=ANY,
    )
    marker = location / VENV_OWNER_FILE
    assert marker.is_file()
    assert json.loads(marker.read_text())["project"] == project.root.as_posix()


def test_conda_backend_create(project, mocker, with_pip):
    assert project.python

    def create_prefix(cmd, **kwargs):
        Path(cmd[cmd.index("--prefix") + 1]).mkdir(parents=True)

    backend = backends.CondaBackend(project, "3.9")
    assert backend.ident == "3.9"
    mock_call = mocker.patch("subprocess.check_call", side_effect=create_prefix)
    location = backend.create(with_pip=with_pip)
    pip_args = ["pip"] if with_pip else []
    mock_call.assert_called_once_with(
        [
            "conda",
            "create",
            "--yes",
            "--prefix",
            str(location),
            "python=3.9",
            *pip_args,
        ],
        stdout=ANY,
    )
    marker = location / VENV_OWNER_FILE
    assert marker.is_file()
    assert json.loads(marker.read_text())["project"] == project.root.as_posix()

    backend = backends.CondaBackend(project, None)
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert backend.ident.startswith(python_version)
    location = backend.create()
    mock_call.assert_called_with(
        [
            "conda",
            "create",
            "--yes",
            "--prefix",
            str(location),
            f"python={python_version}",
        ],
        stdout=ANY,
    )
    assert (location / VENV_OWNER_FILE).is_file()
