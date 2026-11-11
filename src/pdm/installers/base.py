from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Collection, Iterable
from functools import cached_property
from importlib.metadata import Distribution
from itertools import chain

from pdm import termui
from pdm.environments import BaseEnvironment
from pdm.exceptions import BuildError
from pdm.installers.manager import InstallManager
from pdm.installers.recovery import PlannedOperation, SyncRecovery
from pdm.models.candidates import Candidate
from pdm.models.repositories import Package
from pdm.models.requirements import FileRequirement, Requirement, parse_requirement, strip_extras
from pdm.utils import is_editable, is_egg_link, normalize_name


def editables_candidate(environment: BaseEnvironment) -> Candidate | None:
    """Return a candidate for `editables` package"""
    with environment.get_finder() as finder:
        best = finder.find_best_match("editables").best
    return None if best is None else Candidate.from_installation_candidate(best, parse_requirement("editables"))


class BaseSynchronizer:
    """Synchronize the working set with given installation candidates

    :param candidates: a dict of candidates to be installed
    :param environment: the environment associated with the project
    :param clean: clean unneeded packages
    :param dry_run: only prints summary but do not install or uninstall
    :param retry_times: retry times when installation failed
    :param install_self: whether to install self project
    :param no_editable: if True, override all editable installations,
        if a list, override editables with the given names
    :param use_install_cache: whether to use install cache
    :param reinstall: whether to reinstall all packages
    :param only_keep: If true, only keep the selected candidates
    :param fail_fast: If true, stop the installation on first error
    """

    SEQUENTIAL_PACKAGES = ("pip", "setuptools", "wheel")

    def __init__(
        self,
        environment: BaseEnvironment,
        candidates: dict[str, Candidate] | None = None,
        clean: bool = False,
        dry_run: bool = False,
        retry_times: int = 1,
        install_self: bool = False,
        no_editable: bool | Collection[str] = False,
        reinstall: bool = False,
        only_keep: bool = False,
        fail_fast: bool = False,
        use_install_cache: bool | None = None,
        packages: Iterable[Package] = (),
        requirements: Iterable[Requirement] | None = None,
    ) -> None:
        if candidates:  # pragma: no cover
            self.requested_candidates = candidates
        else:
            self.requested_candidates = {entry.candidate.identify(): entry.candidate for entry in packages}
        self.environment = environment
        self.clean = clean
        self.dry_run = dry_run
        self.retry_times = retry_times
        self.no_editable = no_editable
        self.install_self = install_self
        if use_install_cache is None:
            use_install_cache = bool(environment.project.config["install.cache"])
        self.use_install_cache = use_install_cache
        self.reinstall = reinstall
        self.only_keep = only_keep
        self.parallel = environment.project.config["install.parallel"]
        self.fail_fast = fail_fast

        self.working_set = environment.get_working_set()
        self.ui = environment.project.core.ui
        self._manager: InstallManager | None = None
        self.packages = packages
        self.requirements = requirements
        self.recovery = SyncRecovery(environment, self.ui)

    @cached_property
    def self_candidate(self) -> Candidate:
        """Return the candidate for self project"""
        return self.environment.project.make_self_candidate(not self.no_editable)

    @cached_property
    def candidates(self) -> dict[str, Candidate]:
        """Return the candidates to be installed"""
        candidates = self.requested_candidates.copy()
        requested = {
            req.identify()
            for req in (self.requirements or chain.from_iterable(self.environment.project.all_dependencies.values()))
        }
        if isinstance(self.no_editable, Collection):
            keys = self.no_editable
        elif self.no_editable:
            keys = candidates.keys()
        else:
            keys = []
            if self.should_install_editables():
                # Install `editables` as well as required by self project
                editables = editables_candidate(self.environment)
                if editables is not None:
                    candidates["editables"] = editables
        for key in keys:
            if key in candidates and candidates[key].req.editable:
                candidate = candidates[key]
                # Create a new candidate with editable=False
                req = dataclasses.replace(candidate.req, editable=False)
                candidates[key] = candidate.copy_with(req)
        for key in requested:
            if key in candidates:
                candidates[key].requested = True
        return candidates

    def should_install_editables(self) -> bool:
        """Return whether to add editables"""
        if not self.install_self or "editables" in self.requested_candidates:
            return False
        # As editables may be added by the backend, we need to check the metadata
        try:
            metadata = self.self_candidate.prepare(self.environment).metadata
        except BuildError:
            return False
        return any(req.startswith("editables") for req in metadata.requires or [])

    @property
    def manager(self) -> InstallManager:
        if not self._manager:
            self._manager = self.get_manager(rename_pth=True)
        return self._manager

    def get_manager(self, rename_pth: bool = False) -> InstallManager:
        recovery = getattr(self, "recovery", None)
        listener = recovery.record_stash if recovery is not None else None
        return self.environment.project.core.install_manager_class(
            self.environment,
            use_install_cache=self.use_install_cache,
            rename_pth=rename_pth,
            stash_listener=listener,
        )

    def _is_managed_by_pdm(self, dist: Distribution) -> bool:
        """Whether the distribution was installed by PDM and may be cleaned.

        Distributions living in the environment paths but installed by another
        tool (e.g. pip) must never be removed by ``--clean``.
        """
        installer = dist.read_text("INSTALLER")
        if installer is not None and installer.strip() == "pdm":
            return True
        # PEP 582 editable installs made by PDM use egg-link files.
        return is_egg_link(dist)

    @property
    def self_key(self) -> str | None:
        if not self.install_self:
            return None
        name = self.environment.project.name
        if name:
            return normalize_name(name)
        return name

    def _should_update(self, dist: Distribution, can: Candidate) -> bool:
        """Check if the candidate should be updated"""
        backend = self.environment.project.backend
        if self.reinstall or can.req.editable:  # Always update if incoming is editable
            return True
        if is_editable(dist):  # only update editable if no_editable is True
            return bool(self.no_editable)
        if not can.req.is_named:
            dreq = Requirement.from_dist(dist)
            if not isinstance(dreq, FileRequirement):
                return True
            url = dreq.get_full_url()
            if dreq.is_local_dir:
                # We don't know whether a local dir has been changed, always update
                return True
            assert can.link is not None
            if url != backend.expand_line(can.link.url_without_fragment):
                return True
            direct_json = json.loads(content) if (content := dist.read_text("direct_url.json")) else None
            if not direct_json or "archive_info" not in direct_json:
                # We are not able to check, don't update
                return False
            dist_hash = direct_json["archive_info"]["hash"].replace("=", ":")
            return not any(dist_hash == file_hash["hash"] for file_hash in can.hashes)
        specifier = can.req.as_pinned_version(can.version).specifier
        return not specifier.contains(dist.version, prereleases=True)

    def compare_with_working_set(self) -> tuple[list[str], list[str], list[str]]:
        """Compares the candidates and return (to_add, to_update, to_remove)"""
        working_set = self.working_set
        candidates = self.candidates.copy()
        to_update: set[str] = set()
        to_remove: set[str] = set()
        to_add: set[str] = set()
        locked_repository = self.environment.project.get_locked_repository()
        all_candidate_keys = list(locked_repository.all_candidates)

        for key, dist in working_set.items():
            if key == self.self_key and self.install_self:
                continue
            if key in candidates:
                can = candidates.pop(key)
                if self._should_update(dist, can):
                    if working_set.is_owned(key):
                        to_update.add(key)
                    else:
                        to_add.add(key)
            elif (
                (self.only_keep or (self.clean and key not in all_candidate_keys))
                and key not in self.SEQUENTIAL_PACKAGES
                and working_set.is_owned(key)
            ):
                # Remove package only if it is not required by any group
                # Packages for packaging will never be removed
                if self._is_managed_by_pdm(dist):
                    to_remove.add(key)
                else:
                    termui.logger.warning(
                        "Skip removing %s: it is not managed by PDM (no PDM INSTALLER marker), "
                        "use the package's own tooling to uninstall it if needed.",
                        key,
                    )
        to_add.update(
            strip_extras(name)[0]
            for name, _ in candidates.items()
            if name != self.self_key and strip_extras(name)[0] not in working_set
        )
        return (sorted(to_add), sorted(to_update), sorted(to_remove))

    def _build_operations(
        self, to_add: list[str], to_update: list[str], to_remove: list[str]
    ) -> list[PlannedOperation]:
        operations = [
            PlannedOperation("remove", key, self.working_set[key].version) for key in to_remove
        ]
        operations.extend(
            PlannedOperation("update", key, self.candidates[key].version) for key in to_update
        )
        operations.extend(PlannedOperation("add", key, self.candidates[key].version) for key in to_add)
        return operations

    def _self_operation(self) -> PlannedOperation | None:
        if not self.install_self:
            return None
        self_key = self.self_key
        assert self_key
        kind = "update" if self_key in self.working_set else "add"
        return PlannedOperation(kind, self_key, getattr(self.self_candidate, "version", ""), is_self=True)

    def _fingerprint(self, operations: list[PlannedOperation]) -> str:
        payload = {
            "ops": sorted((op.kind, op.key, op.version, op.is_self) for op in operations),
            "clean": self.clean,
            "only_keep": self.only_keep,
        }
        return hashlib.sha224(json.dumps(payload, default=str).encode("utf-8")).hexdigest()

    def _operation_holds(self, op: PlannedOperation, working_set) -> bool:
        """Check whether a journaled "done" operation still matches the disk."""
        if op.kind == "remove":
            return op.key not in working_set
        dist = working_set.get(strip_extras(op.key)[0])
        return dist is not None and dist.version == op.version

    def _prepare_recovery(self, operations: list[PlannedOperation]) -> set[str]:
        """Open or reuse the journal and return ids of verified-done operations."""
        recovery = getattr(self, "recovery", None)
        if recovery is None:
            return set()
        journal = recovery.load()
        fingerprint = self._fingerprint(operations)
        if journal is None or not recovery.fingerprint_matches(fingerprint):
            # Either the first run or a different synchronization target:
            # the previous journal can no longer describe this run.
            recovery.begin(operations, fingerprint)
            return set()
        verified = {
            op.op_id for op in recovery.operations() if op.done and self._operation_holds(op, self.working_set)
        }
        recovery.reset_done(verified)
        return verified

    def synchronize(self) -> None:
        """Synchronize the working set with pinned candidates."""
        recovery = getattr(self, "recovery", None)
        if recovery is not None:
            recovery.recover_environment()
            # The scan may have repaired or removed broken distributions.
            self.working_set = self.environment.get_working_set()
        to_add, to_update, to_remove = self.compare_with_working_set()
        operations = self._build_operations(to_add, to_update, to_remove)
        self_op = self._self_operation()
        if self_op is not None:
            operations.append(self_op)
        done = self._prepare_recovery(operations)
        manager = self.get_manager()
        for key in to_add:
            if f"add:{key}" in done:
                termui.logger.info("Skipping already installed %s@%s", key, self.candidates[key].version)
                continue
            can = self.candidates[key]
            termui.logger.info("Installing %s@%s...", key, can.version)
            manager.install(can)
            if recovery is not None:
                recovery.mark_done("add", key)
        for key in to_update:
            if f"update:{key}" in done:
                termui.logger.info("Skipping already updated %s@%s", key, self.candidates[key].version)
                continue
            can = self.candidates[key]
            dist = self.working_set[strip_extras(key)[0]]
            dist_version = dist.version
            termui.logger.info("Updating %s@%s -> %s...", key, dist_version, can.version)
            manager.overwrite(dist, can)
            if recovery is not None:
                recovery.mark_done("update", key)
        for key in to_remove:
            if f"remove:{key}" in done:
                termui.logger.info("Skipping already removed %s", key)
                continue
            dist = self.working_set[key]
            termui.logger.info("Removing %s@%s...", key, dist.version)
            manager.uninstall(dist)
            if recovery is not None:
                recovery.mark_done("remove", key)
        if self.install_self:
            assert self_op is not None
            self_key = self_op.key
            word = "a" if self.no_editable else "an editable"
            if self_op.op_id not in done:
                termui.logger.info(f"Installing the project as {word} package...")
                if self_key in self.working_set:
                    dist = self.working_set[strip_extras(self_key)[0]]
                    manager.overwrite(dist, self.self_candidate)
                else:
                    manager.install(self.self_candidate)
                if recovery is not None:
                    recovery.mark_done(self_op.kind, self_key)
        if recovery is not None:
            fresh_working_set = self.environment.get_working_set()
            recovery.verify_final_state(self.candidates, fresh_working_set)
            recovery.discard()
        termui.logger.info("Synchronization complete.")
