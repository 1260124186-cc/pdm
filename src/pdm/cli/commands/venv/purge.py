from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pdm import termui
from pdm.cli.commands.base import BaseCommand
from pdm.cli.options import verbose_option

if TYPE_CHECKING:
    from argparse import ArgumentParser, Namespace

    from pdm.cli.commands.venv.utils import CentralVenvRecord
    from pdm.project import Project


class PurgeCommand(BaseCommand):
    """Purge orphaned Virtualenvs whose owner project no longer exists"""

    arguments = (verbose_option,)

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "-f",
            "--force",
            action="store_true",
            help="Force purging without prompting for confirmation",
        )
        parser.add_argument(
            "-i",
            "--interactive",
            action="store_true",
            help="Interactively purge selected Virtualenvs",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Purge every virtualenv under venv.location, including ones whose "
            "ownership cannot be confirmed. Without this flag only orphaned PDM "
            "virtualenvs are removed.",
        )

    def handle(self, project: Project, options: Namespace) -> None:
        if options.all:
            return self.purge_all(project, options)
        return self.purge_orphans(project, options)

    # ------------------------------------------------------------------
    # Ownership-aware reconciliation (default)
    # ------------------------------------------------------------------

    def purge_orphans(self, project: Project, options: Namespace) -> None:
        from pdm.cli.commands.venv.utils import ORPHANED, reconcile_central_venvs

        records = reconcile_central_venvs(project)
        if not records:
            project.core.ui.echo("No virtualenvs to purge, quitting.", style="success")
            return

        self._report_audit(project, records)

        orphans = [record for record in records if record.status == ORPHANED]
        if not orphans:
            project.core.ui.echo(
                "No orphaned virtualenvs found: every entry is still in use or its "
                "ownership cannot be confirmed, quitting.",
                style="success",
            )
            return

        if not options.force and not options.interactive:
            if not termui.confirm("continue?", default=True):
                return
            selected = orphans
        elif options.force:
            selected = orphans
        else:
            selection = termui.ask(
                "Please select",
                choices=([str(i) for i in range(len(orphans))] + ["all", "none"]),
                default="none",
                show_choices=False,
            )
            if selection == "none":
                return
            if selection == "all":
                selected = orphans
            else:
                selected = [orphans[int(selection)]]

        if not selected:
            return
        for record in selected:
            self._remove_venv(project, record.path)
        project.core.ui.echo(
            f"Purged {len(selected)} orphaned virtualenv{'s' if len(selected) > 1 else ''} successfully!"
        )

    @staticmethod
    def _report_audit(project: Project, records: list[CentralVenvRecord]) -> None:
        """Print the full ownership audit before anything is deleted."""
        from pdm.cli.commands.venv.utils import ACTIVE, MANUAL, ORPHANED, UNCONFIRMED

        groups: list[tuple[str, str, str]] = [
            (ORPHANED, "warning", "Orphaned virtualenvs (owner project no longer exists):"),
            (ACTIVE, "success", "Active virtualenvs (owner project still exists), kept:"),
            (UNCONFIRMED, "info", "Unconfirmed entries (ownership cannot be proven), kept:"),
            (MANUAL, "info", "Manually created or non-venv entries, kept:"),
        ]
        for status, style, title in groups:
            matching = [record for record in records if record.status == status]
            if not matching:
                continue
            project.core.ui.echo(f"[{style}]{title}[/]")
            for record in matching:
                owner = f" [dim](owner: {record.owner})[/]" if record.owner is not None else ""
                project.core.ui.echo(f"  [success]{record.name}[/]{owner} [dim]{record.note}[/]")

    # ------------------------------------------------------------------
    # Legacy blanket purge (opt-in via --all)
    # ------------------------------------------------------------------

    def purge_all(self, project: Project, options: Namespace) -> None:
        from pdm.cli.commands.venv.utils import iter_central_venvs

        all_central_venvs = list(iter_central_venvs(project))
        if not all_central_venvs:
            project.core.ui.echo("No virtualenvs to purge, quitting.", style="success")
            return

        if not options.force:
            project.core.ui.echo("The following Virtualenvs will be purged:", style="warning")
            for i, venv in enumerate(all_central_venvs):
                project.core.ui.echo(f"{i}. [success]{venv[0]}[/]")

        if not options.interactive and (options.force or termui.confirm("continue?", default=True)):
            return self.del_all_venvs(project)

        selection = termui.ask(
            "Please select",
            choices=([str(i) for i in range(len(all_central_venvs))] + ["all", "none"]),
            default="none",
            show_choices=False,
        )

        if selection == "all":
            self.del_all_venvs(project)
        elif selection != "none":
            for i, venv in enumerate(all_central_venvs):
                if i == int(selection):
                    self._remove_venv(project, venv[1])
            project.core.ui.echo("Purged successfully!")

    def del_all_venvs(self, project: Project) -> None:
        from pdm.cli.commands.venv.utils import iter_central_venvs

        for _, venv in iter_central_venvs(project):
            self._remove_venv(project, venv)
        project.core.ui.echo("Purged successfully!")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_venv(project: Project, venv: Path) -> None:
        """Remove a central venv and the stale records pointing at it.

        The ownership marker lives inside the venv, so ``rmtree`` removes the
        ownership record in the same operation and an interrupted run can
        simply re-reconcile on the next invocation.
        """
        if venv.is_symlink():
            # rmtree refuses symlinks and would never touch the target.
            venv.unlink()
        else:
            shutil.rmtree(venv)
        saved_python = project._saved_python
        if saved_python and Path(saved_python).parent.parent == venv:
            # Drop the saved interpreter so the project cannot keep reusing a
            # deleted environment through its stale ``.pdm-python`` record.
            project._saved_python = None
