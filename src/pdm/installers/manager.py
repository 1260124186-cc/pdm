from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from importlib.metadata import Distribution
from pathlib import Path
from typing import TYPE_CHECKING

from pdm import termui
from pdm.exceptions import InstallationError, PdmException, UninstallError
from pdm.installers.installers import install_wheel
from pdm.installers.uninstallers import BaseRemovePaths, StashedRemovePaths
from pdm.models.cached_package import CachedPackage
from pdm.utils import is_editable, is_path_relative_to, normalize_name

if TYPE_CHECKING:
    from pdm.environments import BaseEnvironment
    from pdm.models.candidates import Candidate
    from pdm.models.requirements import Requirement


class CachePackageNotFound(PdmException):
    """The cached package referenced by an installation is not available
    in the current package cache and can't be re-linked."""


class InstallManager:
    """The manager that performs the installation and uninstallation actions."""

    def __init__(
        self, environment: BaseEnvironment, *, use_install_cache: bool = False, rename_pth: bool = False
    ) -> None:
        self.environment = environment
        self.use_install_cache = use_install_cache
        self.rename_pth = rename_pth

    def install(self, candidate: Candidate) -> Distribution:
        """Install a candidate into the environment, return the distribution"""
        prepared = candidate.prepare(self.environment)
        dist_info = install_wheel(
            prepared.build(),
            self.environment,
            direct_url=prepared.direct_url(),
            install_links=self.use_install_cache and not candidate.req.editable,
            rename_pth=self.rename_pth,
            requested=candidate.requested,
        )
        return Distribution.at(dist_info)

    def get_paths_to_remove(self, dist: Distribution) -> BaseRemovePaths:
        """Get the path collection to be removed from the disk"""
        return StashedRemovePaths.from_dist(dist, environment=self.environment)

    def uninstall(self, dist: Distribution) -> None:
        """Perform the uninstallation for a given distribution"""
        remove_path = self.get_paths_to_remove(dist)
        dist_name = dist.metadata.get("Name")
        termui.logger.info("Removing distribution %s", dist_name)
        try:
            remove_path.remove()
            remove_path.commit()
        except OSError as e:
            termui.logger.warning("Error occurred during uninstallation, roll back the changes now.")
            remove_path.rollback()
            raise UninstallError(e) from e

    def overwrite(self, dist: Distribution, candidate: Candidate) -> None:
        """An in-place update to overwrite the distribution with a new candidate"""
        paths_to_remove = self.get_paths_to_remove(dist)
        termui.logger.info("Overwriting distribution %s", dist.metadata.get("Name"))
        installed = self.install(candidate)
        installed_paths = self.get_paths_to_remove(installed)
        # Remove the paths that are in the new distribution
        paths_to_remove.difference_update(installed_paths)
        try:
            paths_to_remove.remove()
            paths_to_remove.commit()
        except OSError as e:
            termui.logger.warning("Error occurred during overwriting, roll back the changes now.")
            paths_to_remove.rollback()
            raise UninstallError(e) from e

    # ------------------------------------------------------------------
    # Repair of installations linked into the central package cache
    # ------------------------------------------------------------------

    def iter_site_distributions(self) -> Iterator[Distribution]:
        """Enumerate the distributions by scanning site-packages directly.

        Installations whose cache links are dangling are invisible to the
        normal working set because their METADATA can't be read, so the
        scan must not rely on any metadata content.
        """
        paths = self.environment.get_paths()
        seen: set[Path] = set()
        for scheme in ("platlib", "purelib"):
            root = Path(paths[scheme])
            if not root.is_dir():
                continue
            for dist_info in root.glob("*.dist-info"):
                if dist_info in seen:
                    continue
                seen.add(dist_info)
                yield Distribution.at(os.fspath(dist_info))

    @staticmethod
    def _read_refer_to(dist: Distribution) -> str | None:
        """Return the cached package path recorded in ``REFER_TO`` or None
        when the distribution was not installed as links to the cache."""
        content = dist.read_text("REFER_TO")
        if not content:
            return None
        line = content.strip().splitlines()[0].strip()
        return line or None

    @staticmethod
    def _is_cache_link_broken(dist: Distribution, refer_to: Path) -> bool:
        if not refer_to.exists():
            return True
        for file in dist.files or ():
            location = dist.locate_file(file)
            if location.is_symlink() and not location.exists():
                return True
        return False

    def find_broken_cache_distributions(self) -> list[tuple[Distribution, Path]]:
        """Return ``(distribution, referenced_cache_path)`` pairs for
        cache-linked installations that point to an unavailable cache."""
        broken: list[tuple[Distribution, Path]] = []
        for dist in self.iter_site_distributions():
            if is_editable(dist):
                continue
            refer_to = self._read_refer_to(dist)
            if refer_to is None:
                continue
            refer_to_path = Path(refer_to)
            if self._is_cache_link_broken(dist, refer_to_path):
                broken.append((dist, refer_to_path))
        return broken

    def relink_distribution(self, dist: Distribution, old_cache: Path) -> CachedPackage:
        """Re-bind every linked file of ``dist`` to the matching cached
        package under the current cache root.

        Raises :class:`CachePackageNotFound` when the cached package is
        missing or incomplete; the caller should then reinstall.
        """
        package = self.environment.project.package_cache.find_package(old_cache.name)
        if package is None:
            raise CachePackageNotFound(f"Cached package {old_cache.name} can't be found in the package cache.")
        plan: list[tuple[Path, Path]] = []
        try:
            files = list(dist.files or ())
        except OSError as e:
            raise CachePackageNotFound(f"Can't read the RECORD of {old_cache.name}: {e}") from e
        for file in files:
            installed = Path(dist.locate_file(file))
            if not installed.is_symlink():
                # Regular files (generated scripts, RECORD, INSTALLER, ...)
                # don't depend on the cache; a missing one can't be rebuilt
                # by re-linking, trigger a reinstall instead.
                if not installed.exists():
                    raise CachePackageNotFound(f"Installed file {installed} is missing.")
                continue
            old_target = Path(os.readlink(installed))
            try:
                relative = old_target.relative_to(old_cache)
            except ValueError:
                raise CachePackageNotFound(
                    f"Link {installed} points to {old_target} which is outside the cached package {old_cache}."
                ) from None
            new_target = package.path / relative
            if not new_target.exists():
                raise CachePackageNotFound(f"{relative} is missing from the cached package {package.path}.")
            plan.append((installed, new_target))
        # All targets are verified before touching anything on disk.
        for installed, new_target in plan:
            installed.unlink()
            os.symlink(new_target, installed)
        # Keep the metadata and the cache referrer registry in sync.
        refer_to_file = Path(os.fspath(dist._path)) / "REFER_TO"  # type: ignore[attr-defined]
        refer_to_file.write_text(package.path.as_posix(), encoding="utf-8")
        dist_info_path = os.path.normcase(
            os.path.abspath(os.fspath(dist._path))  # type: ignore[attr-defined]
        )
        package.add_referrer(dist_info_path)
        if old_cache.exists():
            CachedPackage(old_cache).remove_referrer(dist_info_path)
        return package

    @staticmethod
    def _distribution_name_version(dist: Distribution) -> tuple[str, str]:
        dir_name = Path(os.fspath(dist._path)).name  # type: ignore[attr-defined]
        if dir_name.endswith(".dist-info"):
            dir_name = dir_name[: -len(".dist-info")]
        name, _, version = dir_name.partition("-")
        return normalize_name(name), version

    def _reinstall_requirement(self, dist: Distribution, name: str, version: str) -> Requirement:
        from pdm.models.requirements import Requirement, parse_requirement

        if dist.read_text("direct_url.json"):
            with contextlib.suppress(Exception):
                requirement = Requirement.from_dist(dist)
                if requirement.name:
                    return requirement
        return parse_requirement(f"{name}=={version}")

    def _remove_distribution_files(self, dist: Distribution) -> None:
        """Remove a broken installation before reinstalling. The regular
        uninstaller skips dangling symlinks, so the links have to be
        unlinked explicitly here."""
        paths = self.environment.get_paths()
        lib_roots = tuple(Path(paths[scheme]).resolve() for scheme in ("platlib", "purelib"))
        parents: set[Path] = set()
        for file in dist.files or ():
            location = Path(dist.locate_file(file))
            if location.is_symlink() or location.is_file():
                location.unlink()
                parents.add(location.parent)
        dist_info = Path(os.fspath(dist._path))  # type: ignore[attr-defined]
        if dist_info.is_symlink():
            dist_info.unlink()
        elif dist_info.exists():
            shutil.rmtree(dist_info)
            parents.add(dist_info.parent)
        for parent in parents:
            self._prune_empty_dirs(parent, lib_roots)

    @staticmethod
    def _prune_empty_dirs(path: Path, roots: tuple[Path, ...]) -> None:
        current = path
        while current not in roots and any(is_path_relative_to(current, root) for root in roots):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    def reinstall_distribution(self, dist: Distribution) -> Distribution:
        """Reinstall a single distribution whose cache is no longer available."""
        name, version = self._distribution_name_version(dist)
        requirement = self._reinstall_requirement(dist, name, version)
        termui.logger.info("Reinstalling %s%s because its cached package is missing", name, version)
        self._remove_distribution_files(dist)
        if requirement.is_named:
            from pdm.models.candidates import Candidate

            with self.environment.get_finder() as finder:
                best = finder.find_best_match(requirement.as_line()).best
            if best is None:
                raise InstallationError(f"Can't find a candidate to reinstall [success]{name}[/].")
            candidate = Candidate.from_installation_candidate(best, requirement)
            return self.install(candidate)
        # Non-named requirements (URL/VCS/local paths) go through the full
        # resolution pipeline so their sources are handled correctly.
        from pdm.installers.core import install_requirements

        install_requirements([requirement], self.environment, use_install_cache=self.use_install_cache)
        return self.environment.get_working_set()[name]

    def repair_cache_distributions(self) -> list[tuple[str, str]]:
        """Check every cache-linked installation and re-bind it to the
        current package cache. Distributions whose cache is gone for good
        are reinstalled instead.

        Returns a list of ``(name, action)`` pairs where action is
        ``"relinked"`` or ``"reinstalled"``.
        """
        repaired: list[tuple[str, str]] = []
        for dist, old_cache in self.find_broken_cache_distributions():
            name, _ = self._distribution_name_version(dist)
            try:
                self.relink_distribution(dist, old_cache)
            except CachePackageNotFound as e:
                termui.logger.info("Can't re-link %s: %s, reinstalling it", name, e)
                self.reinstall_distribution(dist)
                repaired.append((name, "reinstalled"))
            else:
                repaired.append((name, "relinked"))
        return repaired
