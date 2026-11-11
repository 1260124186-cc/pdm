import abc
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pdm.project import Config, Project
from pdm.project.lockfile import FLAG_CROSS_PLATFORM
from pdm.termui import Verbosity
from pdm.utils import open_for_write_no_symlink, parse_version

if TYPE_CHECKING:
    from pdm.installers.manager import InstallManager


class BaseFixer(abc.ABC):
    """Base class for fixers"""

    # A unique identifier for the fixer
    identifier: str
    # A boolean flag to indicate if the problem is breaking
    breaking: bool = False

    def __init__(self, project: Project) -> None:
        self.project = project

    def log(self, message: str, verbosity: Verbosity = Verbosity.DETAIL) -> None:
        self.project.core.ui.echo(message, verbosity=verbosity)

    @abc.abstractmethod
    def get_message(self) -> str:
        """Return a description of the problem"""

    @abc.abstractmethod
    def fix(self) -> None:
        """Perform the fix"""

    @abc.abstractmethod
    def check(self) -> bool:
        """Check if the problem exists"""


class ProjectConfigFixer(BaseFixer):
    """Fix the project config"""

    identifier = "project-config"

    def get_message(self) -> str:
        return (
            "[success]python.path[/] config needs to be moved to [info].pdm-python[/] and "
            "[info].pdm.toml[/] needs to be renamed to [info]pdm.toml[/]"
        )

    def _fix_gitignore(self) -> None:
        gitignore = self.project.root.joinpath(".gitignore")
        if not gitignore.exists():
            return
        content = gitignore.read_text("utf8")
        if ".pdm-python" not in content:
            content = re.sub(r"^\.pdm\.toml$", ".pdm-python", content, flags=re.MULTILINE)
            with open_for_write_no_symlink(gitignore) as fp:
                fp.write(content)

    def fix(self) -> None:
        old_file = self.project.root.joinpath(".pdm.toml")
        config = Config(old_file).self_data
        python_file = self.project.root.joinpath(".pdm-python")
        # `exists()` follows symlinks, so a dangling link still looks absent here.
        if not python_file.exists() and config.get("python.path"):
            self.log("Creating .pdm-python...", verbosity=Verbosity.DETAIL)
            with open_for_write_no_symlink(python_file) as fp:
                fp.write(config["python.path"])
        # Must stay ahead of the unlink below, so a refusal can't leave `.pdm.toml`
        # behind but no longer ignored.
        self.log("Fixing .gitignore...", verbosity=Verbosity.DETAIL)
        self._fix_gitignore()
        self.project.project_config  # access the project config to move the config items
        self.log("Moving .pdm.toml to pdm.toml...", verbosity=Verbosity.DETAIL)
        old_file.unlink()

    def check(self) -> bool:
        return self.project.root.joinpath(".pdm.toml").exists()


class ProjectPluginFixer(BaseFixer):
    """Move project plugins to the cache directory."""

    identifier = "project-plugins"

    @property
    def old_plugin_dir(self) -> Path:
        return self.project.root / ".pdm-plugins"

    def get_message(self) -> str:
        return "Project plugins need to be moved from [info].pdm-plugins[/] to the PDM cache directory."

    def check(self) -> bool:
        return self.old_plugin_dir.is_dir()

    def fix(self) -> None:
        plugin_root = self.project.project_plugins_dir
        self.log(
            f"Moving .pdm-plugins to {plugin_root.as_posix()}...",
            verbosity=Verbosity.DETAIL,
        )
        plugin_root.parent.mkdir(parents=True, exist_ok=True)
        if plugin_root.exists():
            if plugin_root.is_dir():
                shutil.rmtree(plugin_root)
            else:
                plugin_root.unlink()
        shutil.move(self.old_plugin_dir, plugin_root)


class PackageTypeFixer(BaseFixer):  # pragma: no cover
    identifier = "package-type"

    def get_message(self) -> str:
        package_type = self.project.pyproject.settings["package-type"]
        dist = str(package_type == "library").lower()
        return (
            rf'[success]package-type = "{package_type}"[/] has been renamed to '
            rf"[info]distribution = {dist}[/] under \[tool.pdm] table"
        )

    def check(self) -> bool:
        return "package-type" in self.project.pyproject.settings

    def fix(self) -> None:
        # Copy the project settings
        self.project.pyproject.open_for_write()
        settings = self.project.pyproject.settings.copy()

        # Pop the package type and convert it to a distribution type
        package_type = settings.pop("package-type")
        dist = package_type == "library"
        settings["distribution"] = dist

        # Update the project settings with the new distribution type
        self.project.pyproject._data["tool"].pop("pdm")
        self.project.pyproject.settings.update(settings)

        # Write the updated settings back to the project
        self.project.pyproject.write(False)


class LockStrategyFixer(BaseFixer):
    identifier = "deprecated-cross-platform"

    def get_message(self) -> str:
        return "Lock strategy [success]`cross_platform`[/] has been deprecated in favor of lock targets."

    def check(self) -> bool:
        from pdm.project.lockfile import PDMLock

        lockfile = self.project.lockfile
        if not isinstance(lockfile, PDMLock):  # pragma: no cover
            return False
        lockfile_version = lockfile.file_version
        if not lockfile_version or parse_version(lockfile_version) < parse_version("4.5.0"):
            return False
        return FLAG_CROSS_PLATFORM in lockfile.strategy

    def fix(self) -> None:
        strategies = self.project.lockfile.strategy - {FLAG_CROSS_PLATFORM}
        lockfile = self.project.lockfile.open_for_write()
        lockfile["metadata"]["strategy"] = sorted(strategies)
        self.project.lockfile.write(False)
        self.log("Lock strategy [success]`cross_platform` has been removed.", verbosity=Verbosity.DETAIL)


class CachedPackageFixer(BaseFixer):
    """Re-link installations that point to a moved or missing package cache."""

    identifier = "cached-packages"
    breaking = True

    def _manager(self) -> InstallManager:
        from pdm.installers.manager import InstallManager

        return self.project.core.install_manager_class(
            self.project.environment,
            use_install_cache=bool(self.project.config["install.cache"]),
        )

    def get_message(self) -> str:
        return (
            "Some installed packages are linked to a PDM package cache location that no longer "
            "exists and need to be re-linked to the current cache or reinstalled."
        )

    def check(self) -> bool:
        return bool(self._manager().find_broken_cache_distributions())

    def fix(self) -> None:
        for name, action in self._manager().repair_cache_distributions():
            self.log(
                f"{action.capitalize()} [success]{name}[/] after the package cache relocation.",
                verbosity=Verbosity.DETAIL,
            )
