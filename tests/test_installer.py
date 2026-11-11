from __future__ import annotations

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
from pdm.installers import InstallManager
from pdm.models.cached_package import CachedPackage
from pdm.models.candidates import Candidate
from pdm.models.requirements import parse_requirement
from pdm.project.core import Project
from tests import FIXTURES

pytestmark = pytest.mark.usefixtures("local_finder")


def _candidate(name: str, wheel: str) -> Candidate:
    return Candidate(
        parse_requirement(name),
        link=Link(f"http://fixtures.test/artifacts/{wheel}"),
    )


def _move_cache_root(project: Project, dest: Path) -> None:
    """Simulate moving the whole PDM cache directory to another disk."""
    old_root = Path(project.config["cache_dir"])
    shutil.move(str(old_root), str(dest))
    project.global_config["cache_dir"] = dest.as_posix()


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


def test_repair_relinks_broken_cache_installation(project, pdm):
    installer = InstallManager(project.environment, use_install_cache=True)
    installer.install(_candidate("future-fstrings", "future_fstrings-1.2.0-py2.py3-none-any.whl"))
    lib_path = Path(project.environment.get_paths()["purelib"])
    module = lib_path / "future_fstrings.py"
    assert module.is_symlink() and module.exists()
    cache_name = "future_fstrings-1.2.0-py2.py3-none-any.whl.cache"

    new_cache_root = project.root / "moved-caches"
    _move_cache_root(project, new_cache_root)
    assert module.is_symlink() and not module.exists()
    # The broken distribution is invisible to the plain working set.
    assert "future-fstrings" not in project.environment.get_working_set()

    installer = InstallManager(project.environment, use_install_cache=True)
    broken = installer.find_broken_cache_distributions()
    assert len(broken) == 1
    dist, old_cache = broken[0]
    assert old_cache.name == cache_name

    assert installer.repair_cache_distributions() == [("future-fstrings", "relinked")]

    assert module.exists()
    assert Path(os.readlink(module)).is_relative_to(new_cache_root)
    new_package = project.package_cache.find_package(cache_name)
    assert new_package is not None
    dist_info = lib_path / "future_fstrings-1.2.0.dist-info"
    assert (dist_info / "REFER_TO").read_text() == new_package.path.as_posix()
    assert os.path.normcase(str(dist_info)) in new_package.referrers
    # Imports work again and uninstall keeps functioning.
    result = pdm(["run", "python", "-c", "import future_fstrings"], obj=project)
    assert result.exit_code == 0
    repaired = project.environment.get_working_set()["future-fstrings"]
    installer.uninstall(repaired)
    assert not module.exists()
    assert os.path.normcase(str(dist_info)) not in new_package.referrers


def test_repair_skips_healthy_cache_installation(project):
    installer = InstallManager(project.environment, use_install_cache=True)
    installer.install(_candidate("future-fstrings", "future_fstrings-1.2.0-py2.py3-none-any.whl"))
    lib_path = Path(project.environment.get_paths()["purelib"])
    module = lib_path / "future_fstrings.py"

    assert installer.find_broken_cache_distributions() == []
    assert installer.repair_cache_distributions() == []
    target_before = os.readlink(module)
    installer.repair_cache_distributions()
    assert os.readlink(module) == target_before


def test_repair_skips_non_cached_installation(project):
    InstallManager(project.environment, use_install_cache=False).install(
        _candidate("demo", "demo-0.0.1-py2.py3-none-any.whl")
    )
    lib_path = Path(project.environment.get_paths()["purelib"])
    demo_file = lib_path / "demo.py"
    assert demo_file.is_file() and not demo_file.is_symlink()
    assert not (lib_path / "demo-0.0.1.dist-info" / "REFER_TO").exists()

    installer = InstallManager(project.environment, use_install_cache=True)
    assert installer.find_broken_cache_distributions() == []
    # Even with the cache directory gone, regular (copied) installs are not touched.
    _move_cache_root(project, project.root / "moved-caches")
    assert installer.find_broken_cache_distributions() == []
    assert demo_file.exists()


def test_repair_skips_editable_installation(project, tmp_path_factory):
    editable_path: Path = tmp_path_factory.mktemp("editable-project")
    (editable_path / "setup.py").write_text(
        """
from setuptools import setup

setup(name='editable-project',
      version='0.1.0',
      description='',
      py_modules=['module'],
)
"""
    )
    (editable_path / "module.py").write_text("")
    req = parse_requirement(f"{editable_path.as_uri()}#egg=editable-project", True)
    InstallManager(project.environment, use_install_cache=True).install(Candidate(req))

    installer = InstallManager(project.environment, use_install_cache=True)
    assert installer.find_broken_cache_distributions() == []
    _move_cache_root(project, project.root / "moved-caches")
    assert installer.find_broken_cache_distributions() == []


def test_repair_reinstalls_only_distribution_with_missing_cache(project):
    installer = InstallManager(project.environment, use_install_cache=True)
    installer.install(_candidate("future-fstrings", "future_fstrings-1.2.0-py2.py3-none-any.whl"))
    installer.install(_candidate("jmespath", "jmespath-0.10.0-py2.py3-none-any.whl"))

    new_cache_root = project.root / "moved-caches"
    _move_cache_root(project, new_cache_root)
    # The jmespath cache is lost for good, future-fstrings is still available.
    jmespath_cache = project.package_cache.find_package("jmespath-0.10.0-py2.py3-none-any.whl.cache")
    assert jmespath_cache is not None
    shutil.rmtree(jmespath_cache.path)

    installer = InstallManager(project.environment, use_install_cache=True)
    assert dict(installer.repair_cache_distributions()) == {
        "future-fstrings": "relinked",
        "jmespath": "reinstalled",
    }

    working_set = project.environment.get_working_set()
    assert "future-fstrings" in working_set and "jmespath" in working_set
    lib_path = Path(project.environment.get_paths()["purelib"])
    assert (lib_path / "jmespath").exists()
    assert Path(os.readlink(lib_path / "future_fstrings.py")).is_relative_to(new_cache_root)
    # A fresh cache entry is created for the reinstalled package only.
    assert project.package_cache.find_package("jmespath-0.10.0-py2.py3-none-any.whl.cache") is not None
