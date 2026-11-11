from __future__ import annotations

import base64
import dataclasses as dc
import glob
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from findpython import BaseProvider, PythonVersion

from pdm.exceptions import PdmUsageError
from pdm.models.venv import VirtualEnv
from pdm.utils import open_for_write_no_symlink

if TYPE_CHECKING:
    import sys
    from collections.abc import Iterable

    if sys.version_info >= (3, 11):
        from typing import Self
    else:
        from typing_extensions import Self

    from pdm.project import Project

#: Name of the ownership marker written into every centrally created venv.
#: The marker lives *inside* the venv, so removing the venv removes the record
#: atomically and no separate ledger can ever go out of sync.
VENV_OWNER_FILE = ".pdm-venv.json"

#: Reconciliation statuses. Only ``ORPHANED`` entries may ever be deleted;
#: everything else is retained and reported for the audit.
ORPHANED = "orphaned"
ACTIVE = "active"
UNCONFIRMED = "unconfirmed"
MANUAL = "manual"

#: Matches the legacy ``<project-name>-<8-char-hash>-<ident>`` naming used for
#: PDM venvs created before ownership markers existed. It is only used to label
#: marker-less venvs, never to decide a deletion.
_LEGACY_PDM_VENV_NAME = re.compile(r"^.+-[A-Za-z0-9_-]{8}-.+$")


def hash_path(path: str) -> str:
    """Generate a hash for the given path."""
    return base64.urlsafe_b64encode(hashlib.new("md5", path.encode(), usedforsecurity=False).digest()).decode()[:8]


def write_venv_owner(venv_root: Path, project: Project) -> None:
    """Record the owning project inside a centrally created venv.

    The marker is the authoritative ownership record used when reconciling
    orphaned venvs. It is written through the same symlink-safe helper as
    ``.pdm-python`` so a planted symlink cannot redirect the write.
    """
    payload = {
        "schema": 1,
        "project": project.root.as_posix(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with open_for_write_no_symlink(venv_root / VENV_OWNER_FILE) as fp:
        json.dump(payload, fp)


def read_venv_owner(venv_root: Path) -> tuple[Path | None, bool]:
    """Read the owning project recorded in a venv marker.

    Returns a ``(owner, marker_present)`` tuple. ``marker_present`` being true
    with ``owner`` being ``None`` means the marker exists but cannot be trusted
    (unreadable/corrupt/wrong shape), which must be treated as unconfirmed
    rather than as a manually created directory.
    """
    marker = venv_root / VENV_OWNER_FILE
    if not marker.is_file():
        return None, False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, True
    if not isinstance(data, dict):
        return None, True
    project = data.get("project")
    if not isinstance(project, str) or not project.strip():
        return None, True
    return Path(project), True


@dc.dataclass(frozen=True)
class CentralVenvRecord:
    """Reconciliation result for one entry under ``venv.location``."""

    path: Path
    status: str
    owner: Path | None
    note: str

    @property
    def name(self) -> str:
        return self.path.name


def classify_central_venv(venv_path: Path) -> CentralVenvRecord:
    """Decide the fate of a single entry under ``venv.location``.

    The classification is deliberately conservative: anything that cannot be
    proven to be a PDM venv whose owner project disappeared is kept.
    """
    if venv_path.is_symlink() or not venv_path.is_dir():
        return CentralVenvRecord(venv_path, MANUAL, None, "not a directory, left untouched")

    owner, marker_present = read_venv_owner(venv_path)
    if owner is None:
        if marker_present:
            return CentralVenvRecord(venv_path, UNCONFIRMED, None, "ownership marker is unreadable, left untouched")
        if _LEGACY_PDM_VENV_NAME.match(venv_path.name):
            return CentralVenvRecord(
                venv_path,
                UNCONFIRMED,
                None,
                "looks like a PDM venv but has no ownership marker, left untouched",
            )
        return CentralVenvRecord(venv_path, MANUAL, None, "no PDM ownership marker, left untouched")

    if owner.is_dir():
        return CentralVenvRecord(venv_path, ACTIVE, owner, "owner project still exists")
    if owner.exists():
        return CentralVenvRecord(venv_path, UNCONFIRMED, owner, "owner path is not a directory, left untouched")
    return CentralVenvRecord(venv_path, ORPHANED, owner, "owner project no longer exists")


def reconcile_central_venvs(project: Project) -> list[CentralVenvRecord]:
    """Audit every entry under ``venv.location`` against its ownership marker.

    The scan is read-only and returns entries sorted by name so repeated runs
    after an interruption converge on the same result deterministically.
    """
    venv_parent = get_venv_parent(project)
    if not venv_parent.is_dir():
        return []
    return [classify_central_venv(venv) for venv in sorted(venv_parent.iterdir(), key=lambda p: p.name)]


def get_in_project_venv(root: Path) -> VirtualEnv | None:
    """Get the python interpreter path of venv-in-project"""
    for possible_dir in (".venv", "venv", "env"):
        venv = VirtualEnv.get(root / possible_dir)
        if venv is not None:
            return venv
    return None


def get_venv_prefix(project: Project) -> str:
    """Get the venv prefix for the project"""
    path = project.root
    name_hash = hash_path(path.as_posix())
    return f"{path.name}-{name_hash}-"


def get_venv_parent(project: Project) -> Path:
    """Get the parent directory holding the centrally-created venvs.

    ``venv.location`` may contain a `~` prefix, which must be expanded the same
    way here as it is when the venv is created, otherwise the venvs are created
    in one directory and looked up in another.
    """
    return Path(project.config["venv.location"]).expanduser()


def iter_venvs(project: Project) -> Iterable[tuple[str, VirtualEnv]]:
    """Return an iterable of venv paths associated with the project"""
    in_project_venv = get_in_project_venv(project.root)
    if in_project_venv is not None:
        yield "in-project", in_project_venv
    venv_prefix = get_venv_prefix(project)
    venv_parent = get_venv_parent(project)
    # The project directory name is part of the prefix and may legally contain
    # glob metacharacters (e.g. `my[project]`), which would otherwise be treated
    # as a pattern and fail to match the venv that `venv create` wrote to disk.
    for path in venv_parent.glob(f"{glob.escape(venv_prefix)}*"):
        ident = path.name[len(venv_prefix) :]
        venv = VirtualEnv.get(path)
        if venv is not None:
            yield ident, venv


def iter_central_venvs(project: Project) -> Iterable[tuple[str, Path]]:
    """Return an iterable of all managed venvs and their paths."""
    venv_parent = get_venv_parent(project)
    for venv in venv_parent.glob("*"):
        ident = venv.name
        yield ident, venv


class VenvProvider(BaseProvider):
    """A Python provider for project venv pythons"""

    def __init__(self, project: Project) -> None:
        self.project = project

    @classmethod
    def create(cls) -> Self | None:
        return None

    def find_pythons(self) -> Iterable[PythonVersion]:
        for _, venv in iter_venvs(self.project):
            yield PythonVersion(venv.interpreter, _interpreter=venv.interpreter, keep_symlink=True)


def get_venv_with_name(project: Project, name: str) -> VirtualEnv:
    all_venvs = dict(iter_venvs(project))
    try:
        return all_venvs[name]
    except KeyError:
        raise PdmUsageError(
            f"No virtualenv with key '{name}' is found, must be one of {list(all_venvs)}.\n"
            "You can create one with 'pdm venv create'.",
        ) from None
