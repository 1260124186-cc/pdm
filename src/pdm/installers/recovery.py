"""Recovery support for hard-interrupted synchronizations.

When a ``pdm sync`` process is killed (for instance by the OOM killer in CI),
no ``finally`` block runs, so the environment can be left with:

* ``.pth.pdmtmp`` files that were never renamed into active ``.pth`` files;
* half-written ``.dist-info`` directories (missing ``METADATA``/``RECORD`` or
  with recorded files missing from disk);
* uninstall stash directories in the temporary folder holding files that were
  moved away but never committed.

The :class:`SyncRecovery` journal (``.pdm-sync-recovery.json`` under the
environment prefix) records the planned operations, their completion state and
the uninstall stashes that are in flight. Every journal update is atomic, so a
re-run can either resume from the verified state or stop with a clear error
while keeping the environment retryable.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pdm import termui
from pdm.exceptions import InstallationError
from pdm.installers.uninstallers import renames
from pdm.utils import is_path_relative_to, normalize_name

if TYPE_CHECKING:
    from pdm.environments import BaseEnvironment

JOURNAL_NAME = ".pdm-sync-recovery.json"
JOURNAL_VERSION = 1
STASH_PREFIX = "pdm"
STASH_SUFFIX = "-uninstall"
PDM_INSTALLER = "pdm"


@dataclasses.dataclass
class PlannedOperation:
    """A single add/update/remove operation tracked by the journal."""

    kind: str
    key: str
    version: str
    is_self: bool = False
    done: bool = False

    @property
    def op_id(self) -> str:
        return f"{self.kind}:{self.key}"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlannedOperation:
        return cls(
            kind=data["kind"],
            key=data["key"],
            version=data.get("version", ""),
            is_self=data.get("is_self", False),
            done=data.get("done", False),
        )


class SyncRecovery:
    """Persists synchronization progress and repairs interrupted environments."""

    def __init__(self, environment: BaseEnvironment, ui: Any) -> None:
        self.environment = environment
        self.ui = ui
        self._lock = threading.Lock()
        self._journal: dict[str, Any] | None = None
        self._loaded = False

    # ------------------------------------------------------------------ paths
    @property
    def prefix(self) -> Path:
        return Path(self.environment.get_paths()["prefix"])

    @property
    def journal_path(self) -> Path:
        return self.prefix / JOURNAL_NAME

    def _lib_dirs(self) -> list[Path]:
        paths = self.environment.get_paths()
        result: list[Path] = []
        for scheme in ("purelib", "platlib"):
            path = Path(paths[scheme])
            if path not in result:
                result.append(path)
        return result

    # ---------------------------------------------------------------- journal
    def load(self) -> dict[str, Any] | None:
        """Load the journal from disk, caching the result."""
        if self._loaded:
            return self._journal
        self._loaded = True
        path = self.journal_path
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise InstallationError(
                f"The recovery journal {path} is unreadable ({e}). Fix or remove it manually, "
                "then rerun the sync."
            ) from e
        if data.get("version") != JOURNAL_VERSION:
            termui.logger.warning("Discarding recovery journal with unsupported version: %s", path)
            self.discard()
            return None
        self._journal = data
        return data

    def _write(self, data: dict[str, Any]) -> None:
        """Atomically replace the journal file."""
        path = self.journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{JOURNAL_NAME}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        self._journal = data
        self._loaded = True

    def discard(self) -> None:
        """Remove the journal once the synchronization completed successfully."""
        with self._lock:
            try:
                self.journal_path.unlink()
            except FileNotFoundError:
                pass
            tmp = self.journal_path.with_name(f"{JOURNAL_NAME}.tmp")
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            self._journal = None
            self._loaded = True

    def begin(self, operations: list[PlannedOperation], fingerprint: str) -> None:
        """Persist a fresh plan, all operations starting as pending."""
        with self._lock:
            data = {
                "version": JOURNAL_VERSION,
                "fingerprint": fingerprint,
                "operations": [op.to_dict() for op in operations],
                "stashes": list(self._journal.get("stashes", [])) if self._journal else [],
            }
            self._write(data)

    def fingerprint_matches(self, fingerprint: str) -> bool:
        return bool(self._journal and self._journal.get("fingerprint") == fingerprint)

    def operations(self) -> list[PlannedOperation]:
        if not self._journal:
            return []
        return [PlannedOperation.from_dict(item) for item in self._journal.get("operations", [])]

    def done_ids(self) -> set[str]:
        return {op.op_id for op in self.operations() if op.done}

    def reset_done(self, valid_ids: set[str]) -> None:
        """Mark operations as pending again when their state no longer holds."""
        with self._lock:
            if not self._journal:
                return
            changed = False
            for item in self._journal.get("operations", []):
                if item.get("done") and f"{item['kind']}:{item['key']}" not in valid_ids:
                    item["done"] = False
                    changed = True
            if changed:
                self._write(self._journal)

    def mark_done(self, kind: str, key: str) -> None:
        """Record an operation as completed, flushing the journal atomically."""
        with self._lock:
            if not self._journal:
                return
            changed = False
            for item in self._journal.get("operations", []):
                if item["kind"] == kind and item["key"] == key and not item.get("done"):
                    item["done"] = True
                    changed = True
            if changed:
                self._write(self._journal)

    def record_stash(self, tempdir: str, root: str) -> None:
        """Write-ahead log a stash directory before any file is moved into it."""
        with self._lock:
            if not self._journal:
                return
            stashes = self._journal.setdefault("stashes", [])
            entry = {"tempdir": tempdir, "root": root}
            if entry not in stashes:
                stashes.append(entry)
                self._write(self._journal)

    def _remove_stash_record(self, tempdir: str) -> None:
        with self._lock:
            if not self._journal:
                return
            stashes = self._journal.get("stashes", [])
            remaining = [item for item in stashes if item["tempdir"] != tempdir]
            if len(remaining) != len(stashes):
                self._journal["stashes"] = remaining
                self._write(self._journal)

    def planned_keys(self) -> set[str]:
        return {op.key for op in self.operations()}

    # --------------------------------------------------------- disk recovery
    def recover_environment(self) -> None:
        """Repair every leftover a hard kill may have produced.

        The disk, not the journal, is the source of truth here. The journal is
        only used to attribute ambiguous artifacts to PDM. Anything that cannot
        be repaired safely aborts with :class:`InstallationError` and leaves
        the files untouched so the state stays retryable.
        """
        journal = self.load()
        planned_keys = self.planned_keys()
        self._recover_pth_tmp_files()
        self._recover_partial_distributions(planned_keys)
        self._rollback_stash_directories(journal)

    def _recover_pth_tmp_files(self) -> None:
        """Finalize postponed ``.pth`` writes from an interrupted install."""
        for lib in self._lib_dirs():
            if not lib.is_dir():
                continue
            for tmp_path in lib.glob("*.pdmtmp"):
                target = tmp_path.with_suffix("")
                if not target.exists():
                    termui.logger.info("Recovering postponed pth file %s", target)
                    tmp_path.rename(target)
                    continue
                if target.read_bytes() == tmp_path.read_bytes():
                    # The final file already exists, the leftover copy is redundant.
                    tmp_path.unlink()
                    continue
                raise InstallationError(
                    f"Both {tmp_path} and {target} exist with different contents. Refusing to guess "
                    "which one is correct; please inspect them manually and rerun the sync."
                )

    def _recover_partial_distributions(self, planned_keys: set[str]) -> None:
        """Remove half-installed distributions so they get installed again."""
        for lib in self._lib_dirs():
            if not lib.is_dir():
                continue
            for dist_info in lib.glob("*.dist-info"):
                problem = self._dist_info_problem(lib, dist_info)
                if problem is None:
                    continue
                name = normalize_name(dist_info.name.split("-", 1)[0])
                if not self._is_pdm_attributed(dist_info, name, planned_keys):
                    raise InstallationError(
                        f"Found incomplete distribution metadata {dist_info} ({problem}) that is not "
                        "marked as managed by PDM. Nothing was removed. Please repair or delete it "
                        "manually, then rerun the sync."
                    )
                termui.logger.warning("Removing incomplete distribution %s (%s)", dist_info, problem)
                self._purge_partial_distribution(lib, dist_info)

    def _dist_info_problem(self, lib: Path, dist_info: Path) -> str | None:
        if not (dist_info / "METADATA").is_file():
            return "missing METADATA"
        record = dist_info / "RECORD"
        if not record.is_file():
            return "missing RECORD"
        try:
            from installer.records import parse_record_file

            rows = parse_record_file(record.read_text(encoding="utf-8", errors="replace").splitlines())
        except Exception as e:  # malformed RECORD, installer API is not narrowed
            return f"unreadable RECORD ({e})"
        for rel_path, _hash, _size in rows:
            if rel_path.endswith("RECORD"):
                continue
            if not (lib / rel_path).exists():
                return f"recorded file {rel_path} is missing"
        return None

    def _is_pdm_attributed(self, dist_info: Path, name: str, planned_keys: set[str]) -> bool:
        installer_file = dist_info / "INSTALLER"
        if installer_file.is_file():
            try:
                if installer_file.read_text(encoding="utf-8").strip() == PDM_INSTALLER:
                    return True
            except OSError:
                pass
        # The journal proves PDM was installing/updating this distribution when
        # the process was killed, even if the INSTALLER marker wasn't written.
        return name in planned_keys

    def _purge_partial_distribution(self, lib: Path, dist_info: Path) -> None:
        record = dist_info / "RECORD"
        if record.is_file():
            try:
                from installer.records import parse_record_file

                rows = parse_record_file(record.read_text(encoding="utf-8", errors="replace").splitlines())
            except Exception:
                rows = []
            prefix = self.prefix
            for rel_path, _hash, _size in rows:
                target = lib / rel_path
                if not is_path_relative_to(target, prefix):
                    # Never delete files outside of the environment prefix.
                    continue
                try:
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                except OSError as e:
                    raise InstallationError(
                        f"Failed to remove leftover file {target} of the incomplete distribution: {e}"
                    ) from e
        shutil.rmtree(dist_info, ignore_errors=True)

    def _rollback_stash_directories(self, journal: dict[str, Any] | None) -> None:
        """Restore files stashed by interrupted uninstalls/updates."""
        recorded: dict[str, str] = {}
        for item in (journal or {}).get("stashes", []):
            recorded[item["tempdir"]] = item["root"]

        for tempdir, root in recorded.items():
            temp_path = Path(tempdir)
            if not temp_path.is_dir():
                # The OS already reaped the stash; the missing files are
                # re-created by re-running the planned operation.
                self._remove_stash_record(tempdir)
                continue
            self._restore_stash(temp_path, Path(root))
            self._remove_stash_record(tempdir)

        leftovers = {
            str(path)
            for path in Path(tempfile.gettempdir()).glob(f"{STASH_PREFIX}*{STASH_SUFFIX}*")
            if path.is_dir()
        } - set(recorded)
        if leftovers:
            details = "\n".join(sorted(leftovers))
            raise InstallationError(
                "Found PDM uninstall stash directories without a matching recovery record:\n"
                f"{details}\nMove their contents back under {self.prefix} manually (or remove the "
                "directories if they are stale), then rerun the sync. No files were changed."
            )

    def _restore_stash(self, temp_path: Path, root: Path) -> None:
        for src in sorted(path for path in temp_path.rglob("*") if path.is_file() or path.is_symlink()):
            rel_path = src.relative_to(temp_path)
            target = root / rel_path
            if target.exists():
                if target.is_file() and target.read_bytes() == src.read_bytes():
                    src.unlink()
                    continue
                raise InstallationError(
                    f"Cannot restore stashed file {src}: {target} already exists with different "
                    "contents. Refusing to overwrite it; resolve the conflict and rerun the sync."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            renames(str(src), str(target))
        shutil.rmtree(temp_path, ignore_errors=True)

    # ------------------------------------------------------------ verification
    def verify_final_state(self, candidates: dict[str, Any], working_set: Any) -> None:
        """Confirm the claimed result matches what is actually on disk.

        Raises :class:`InstallationError` (keeping the journal) whenever a
        package is missing, a distribution is incomplete or postponed ``.pth``
        files are still present.
        """
        for lib in self._lib_dirs():
            if lib.is_dir() and any(lib.glob("*.pdmtmp")):
                raise InstallationError(
                    f"Postponed .pth files remain in {lib}; the synchronization cannot be marked complete."
                )
        problems: list[str] = []
        for key, candidate in candidates.items():
            from pdm.models.requirements import strip_extras

            real_key = strip_extras(key)[0]
            if real_key not in working_set:
                problems.append(f"{real_key} is not importable after installation")
                continue
            dist = working_set[real_key]
            dist_info = getattr(dist, "_path", None)
            if dist_info is not None and self._dist_info_problem(Path(dist_info).parent, Path(dist_info)):
                problems.append(f"{real_key} has incomplete distribution metadata")
        if problems:
            raise InstallationError(
                "The synchronized environment does not match the resolved packages:\n- "
                + "\n- ".join(problems)
            )
