from __future__ import annotations

import json
import logging
import os
import shutil
import venv
from collections.abc import Callable
from pathlib import Path

import pytest
from unearth import Link

from pdm import utils
from pdm.core import Core
from pdm.environments.base import BaseEnvironment
from pdm.environments.local import PythonLocalEnvironment
from pdm.environments.python import PythonEnvironment
from pdm.exceptions import RequirementError
from pdm.installers import InstallManager
from pdm.installers.base import BaseSynchronizer
from pdm.models.cached_package import CachedPackage
from pdm.models.candidates import Candidate
from pdm.models.requirements import parse_requirement
from pdm.project.core import Project
from tests import FIXTURES

pytestmark = pytest.mark.usefixtures("local_finder")


@pytest.fixture()
def supports_link(preferred: str | None, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], bool]:
    original = utils.fs_supports_link_method

    def mocked_support(linker: str) -> bool:
        if preferred is None:
            return False
        if preferred == "hardlink" and linker == "symlink":
            return False
        return original(linker)

    monkeypatch.setattr(utils, "fs_supports_link_method", mocked_support)
    return mocked_support


def _prepare_project_for_env(project: Project, env_cls: type[BaseEnvironment]):
    project._saved_python = None
    project._python = None
    if env_cls is PythonEnvironment:
        venv.create(project.root / ".venv", symlinks=True)
        project.project_config["python.use_venv"] = True


@pytest.fixture(params=(PythonEnvironment, PythonLocalEnvironment), autouse=True)
def environment(request: pytest.RequestFixture, project: Project) -> type[BaseEnvironment]:
    # Run all test against all environments as installation and cache behavior may differ
    env_cls: type[BaseEnvironment] = request.param
    _prepare_project_for_env(project, env_cls)
    return env_cls


def test_install_wheel_with_inconsistent_dist_info(project):
    req = parse_requirement("pyfunctional")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/PyFunctional-1.4.3-py3-none-any.whl"),
    )
    installer = InstallManager(project.environment)
    installer.install(candidate)
    assert "pyfunctional" in project.environment.get_working_set()


def test_install_with_file_existing(project):
    req = parse_requirement("demo")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/demo-0.0.1-py2.py3-none-any.whl"),
    )
    lib_path = project.environment.get_paths()["purelib"]
    os.makedirs(lib_path, exist_ok=True)
    with open(os.path.join(lib_path, "demo.py"), "w") as fp:
        fp.write("print('hello')\n")
    installer = InstallManager(project.environment)
    installer.install(candidate)


def test_uninstall_commit_rollback(project):
    req = parse_requirement("demo")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/demo-0.0.1-py2.py3-none-any.whl"),
    )
    installer = InstallManager(project.environment)
    lib_path = project.environment.get_paths()["purelib"]
    installer.install(candidate)
    lib_file = os.path.join(lib_path, "demo.py")
    assert os.path.exists(lib_file)
    remove_paths = installer.get_paths_to_remove(project.environment.get_working_set()["demo"])
    remove_paths.remove()
    assert not os.path.exists(lib_file)
    remove_paths.rollback()
    assert os.path.exists(lib_file)


def test_rollback_after_commit(project, caplog):
    caplog.set_level(logging.ERROR, logger="pdm.termui")
    logging.getLogger("pdm.termui").addHandler(caplog.handler)
    req = parse_requirement("demo")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/demo-0.0.1-py2.py3-none-any.whl"),
    )
    installer = InstallManager(project.environment)
    lib_path = project.environment.get_paths()["purelib"]
    installer.install(candidate)
    lib_file = os.path.join(lib_path, "demo.py")
    assert os.path.exists(lib_file)
    remove_paths = installer.get_paths_to_remove(project.environment.get_working_set()["demo"])
    remove_paths.remove()
    remove_paths.commit()
    assert not os.path.exists(lib_file)
    caplog.clear()
    remove_paths.rollback()
    assert not os.path.exists(lib_file)

    assert any(message == "Can't rollback, not uninstalled yet" for message in caplog.messages)


@pytest.mark.parametrize("use_install_cache", [False, True])
def test_uninstall_with_console_scripts(project, use_install_cache):
    req = parse_requirement("celery")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/celery-4.4.2-py2.py3-none-any.whl"),
    )
    installer = InstallManager(project.environment, use_install_cache=use_install_cache)
    installer.install(candidate)
    celery_script = os.path.join(
        project.environment.get_paths()["scripts"],
        "celery.exe" if os.name == "nt" else "celery",
    )
    assert os.path.exists(celery_script)
    installer.uninstall(project.environment.get_working_set()["celery"])
    assert not os.path.exists(celery_script)


@pytest.mark.parametrize("preferred", ["symlink", "hardlink", None])
def test_install_wheel_with_cache(project, pdm, supports_link):
    req = parse_requirement("future-fstrings")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/future_fstrings-1.2.0-py2.py3-none-any.whl"),
    )
    installer = InstallManager(project.environment, use_install_cache=True)
    installer.install(candidate)

    lib_path = project.environment.get_paths()["purelib"]
    if supports_link("symlink"):
        assert os.path.islink(os.path.join(lib_path, "future_fstrings.py"))
        assert os.path.islink(os.path.join(lib_path, "aaaaa_future_fstrings.pth"))
    else:
        assert os.path.isfile(os.path.join(lib_path, "future_fstrings.py"))
        assert os.path.isfile(os.path.join(lib_path, "aaaaa_future_fstrings.pth"))

    for file in CachedPackage.cache_files:
        assert not os.path.exists(os.path.join(lib_path, file))

    cache_name = "future_fstrings-1.2.0-py2.py3-none-any.whl.cache"
    assert any(p.path.name == cache_name for p in project.package_cache.iter_packages())
    pdm(["run", "python", "-m", "site"], object=project)
    r = pdm(["run", "python", "-c", "import future_fstrings"], obj=project)
    assert r.exit_code == 0
    pdm(["cache", "clear", "packages"], obj=project, strict=True)
    assert supports_link("symlink") is any(p.path.name == cache_name for p in project.package_cache.iter_packages())

    dist = project.environment.get_working_set()["future-fstrings"]
    installer.uninstall(dist)
    assert not os.path.exists(os.path.join(lib_path, "future_fstrings.py"))
    assert not os.path.exists(os.path.join(lib_path, "aaaaa_future_fstrings.pth"))
    assert not dist.read_text("direct_url.json")

    pdm(["cache", "clear", "packages"], obj=project, strict=True)
    assert not any(p.path.name == cache_name for p in project.package_cache.iter_packages())


@pytest.mark.parametrize("preferred", ["symlink", "hardlink", None])
def test_can_install_wheel_with_cache_in_multiple_projects(
    project: Project, core: Core, supports_link, tmp_path_factory, environment
):
    projects = []

    for idx in range(3):
        path: Path = tmp_path_factory.mktemp(f"project-{idx}")
        p = core.create_project(path, global_config=project.global_config.config_file)
        _prepare_project_for_env(p, environment)
        projects.append(p)

    req = parse_requirement("future-fstrings")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/future_fstrings-1.2.0-py2.py3-none-any.whl"),
    )

    for p in projects:
        installer = InstallManager(p.environment, use_install_cache=True)
        installer.install(candidate)

        lib_path = p.environment.get_paths()["purelib"]
        if supports_link("symlink"):
            assert os.path.islink(os.path.join(lib_path, "future_fstrings.py"))
            assert os.path.islink(os.path.join(lib_path, "aaaaa_future_fstrings.pth"))
        else:
            assert os.path.isfile(os.path.join(lib_path, "future_fstrings.py"))
            assert os.path.isfile(os.path.join(lib_path, "aaaaa_future_fstrings.pth"))

        for file in CachedPackage.cache_files:
            assert not os.path.exists(os.path.join(lib_path, file))


def test_url_requirement_is_not_cached(project):
    req = parse_requirement(
        "future-fstrings @ http://fixtures.test/artifacts/future_fstrings-1.2.0-py2.py3-none-any.whl"
    )
    candidate = Candidate(req)
    installer = InstallManager(project.environment, use_install_cache=True)
    installer.install(candidate)
    cache_path = project.cache("packages") / "future_fstrings-1.2.0-py2.py3-none-any"
    assert not cache_path.is_dir()
    lib_path = project.environment.get_paths()["purelib"]
    assert os.path.isfile(os.path.join(lib_path, "future_fstrings.py"))
    assert os.path.isfile(os.path.join(lib_path, "aaaaa_future_fstrings.pth"))
    dist = project.environment.get_working_set()["future-fstrings"]
    assert dist.read_text("direct_url.json")


def test_editable_is_not_cached(project, tmp_path_factory):
    editable_path: Path = tmp_path_factory.mktemp("editable-project")

    editable_setup = editable_path / "setup.py"
    editable_setup.write_text("""
from setuptools import setup

setup(name='editable-project',
      version='0.1.0',
      description='',
      py_modules=['module'],
)
""")
    editable_module = editable_path / "module.py"
    editable_module.write_text("")

    req = parse_requirement(f"{editable_path.as_uri()}#egg=editable-project", True)
    candidate = Candidate(req)
    installer = InstallManager(project.environment, use_install_cache=True)
    installer.install(candidate)

    cache_path = project.cache("packages") / "editable_project-0.1.0-0.editable-py3-none-any.whl.cache"
    assert not cache_path.is_dir()
    lib_path = Path(project.environment.get_paths()["purelib"])
    for pth in lib_path.glob("*editable_project*.pth"):
        assert pth.is_file()
        assert not pth.is_symlink()


def _make_local_editable(path: Path, name: str, marker: str) -> Path:
    path.mkdir(parents=True)
    path.joinpath("setup.py").write_text(
        f"from setuptools import setup\nsetup(name={name!r}, version='0.1.0', py_modules=['module'])\n"
    )
    path.joinpath("module.py").write_text(f"VALUE = {marker!r}\n")
    return path


def _editable_candidate(path: Path, name: str) -> Candidate:
    return Candidate(parse_requirement(f"{path.as_uri()}#egg={name}", True))


def test_editable_sync_reconciles_moved_source(project, tmp_path):
    env = project.environment
    manager = InstallManager(env)
    foo_source = _make_local_editable(tmp_path / "packages" / "foo", "foo", "old")
    bar_source = _make_local_editable(tmp_path / "packages" / "bar", "bar", "bar")
    foo = _editable_candidate(foo_source, "foo")
    bar = _editable_candidate(bar_source, "bar")
    manager.install(foo)
    manager.install(bar)

    def make_synchronizer(candidates):
        return BaseSynchronizer(env, candidates=candidates)

    # Both editable records match the locked sources: nothing to do.
    assert make_synchronizer({"foo": foo, "bar": bar}).compare_with_working_set() == ([], [], [])

    # Move only foo's source directory; bar's source stays where it was.
    new_foo_source = tmp_path / "packages" / "foo-moved"
    shutil.move(foo_source, new_foo_source)
    new_foo = _editable_candidate(new_foo_source, "foo")
    to_add, to_update, to_remove = make_synchronizer({"foo": new_foo, "bar": bar}).compare_with_working_set()
    assert to_update == ["foo"]
    assert not to_add and not to_remove

    # Apply the sync: foo is reinstalled from the new source, bar is untouched.
    bar_dist_info = env.get_working_set()["bar"]._path
    make_synchronizer({"foo": new_foo, "bar": bar}).synchronize()
    assert env.get_working_set()["bar"]._path == bar_dist_info
    foo_dist = env.get_working_set()["foo"]
    assert json.loads(foo_dist.read_text("direct_url.json"))["url"] == new_foo_source.as_uri()

    # A follow-up sync converges to a no-op.
    assert make_synchronizer({"foo": new_foo, "bar": bar}).compare_with_working_set() == ([], [], [])

    # A locked source that disappeared must fail loudly instead of claiming
    # the environment is up to date.
    shutil.rmtree(new_foo_source)
    with pytest.raises(RequirementError, match="does not exist"):
        make_synchronizer({"foo": new_foo, "bar": bar}).compare_with_working_set()


def test_editable_overwrite_rolls_back_after_interruption(project, tmp_path, mocker):
    env = project.environment
    manager = InstallManager(env)
    source = _make_local_editable(tmp_path / "pkg", "pkg", "old")
    candidate = _editable_candidate(source, "pkg")
    manager.install(candidate)
    dist_before = env.get_working_set()["pkg"]
    record_before = dist_before.read_text("direct_url.json")
    purelib = Path(env.get_paths()["purelib"])

    new_source = _make_local_editable(tmp_path / "pkg-new", "pkg", "new")
    new_candidate = _editable_candidate(new_source, "pkg")

    real_install = InstallManager.install
    state = {"failed": False}

    def interrupt_once(self, candidate):
        if not state["failed"]:
            state["failed"] = True
            raise KeyboardInterrupt
        return real_install(self, candidate)

    mocker.patch.object(InstallManager, "install", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        manager.overwrite(dist_before, new_candidate)

    # The old distribution is restored and no duplicate dist-info is left.
    dist_after = env.get_working_set()["pkg"]
    assert dist_after.read_text("direct_url.json") == record_before
    assert len(list(purelib.glob("pkg-*.dist-info"))) == 1

    # Re-running the overwrite converges to the new source with a single record.
    manager.overwrite(dist_after, new_candidate)
    dist_final = env.get_working_set()["pkg"]
    assert json.loads(dist_final.read_text("direct_url.json"))["url"] == new_source.as_uri()
    assert len(list(purelib.glob("pkg-*.dist-info"))) == 1


@pytest.mark.parametrize("use_install_cache", [False, True])
def test_install_wheel_with_data_scripts(project, use_install_cache):
    req = parse_requirement("jmespath")
    candidate = Candidate(
        req,
        link=Link("http://fixtures.test/artifacts/jmespath-0.10.0-py2.py3-none-any.whl"),
    )
    installer = InstallManager(project.environment, use_install_cache=use_install_cache)
    installer.install(candidate)
    bin_path = os.path.join(project.environment.get_paths()["scripts"], "jp.py")
    assert os.path.isfile(bin_path)
    if os.name != "nt":
        assert os.stat(bin_path).st_mode & 0o100

    dist = project.environment.get_working_set()["jmespath"]
    installer.uninstall(dist)
    assert not os.path.exists(bin_path)


def test_windows_record_uses_relative_paths_for_data_scripts(tmp_path, mocker):
    from installer.records import RecordEntry

    from pdm.installers.installers import InstallDestination

    mocker.patch("pdm.installers.installers._WINDOWS", True)
    destination = InstallDestination(
        scheme_dict={
            "purelib": str(tmp_path / "Lib" / "site-packages"),
            "scripts": str(tmp_path / "Scripts"),
        },
        interpreter="python",
        script_kind="posix",
    )

    destination.finalize_installation(
        "purelib",
        "jmespath-0.10.0.dist-info/RECORD",
        [("scripts", RecordEntry("jp.py", None, 1685))],
    )

    record = tmp_path.joinpath("Lib", "site-packages", "jmespath-0.10.0.dist-info", "RECORD").read_text()
    assert record.splitlines()[0] == "../../Scripts/jp.py,,1685"


def test_compress_file_list_for_rename():
    from pdm.installers.uninstallers import compress_for_rename

    project_root = str(FIXTURES / "projects")

    paths = {
        "test-removal/subdir",
        "test-removal/subdir/__init__.py",
        "test-removal/__init__.py",
        "test-removal/bar.py",
        "test-removal/foo.py",
        "test-removal/non_exist.py",
    }
    abs_paths = {os.path.join(project_root, path) for path in paths}
    assert sorted(compress_for_rename(abs_paths)) == [os.path.join(project_root, "test-removal" + os.sep)]
