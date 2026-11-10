#!/usr/bin/env python3
"""Unit tests for backup_docker_compose_projects.py. No Docker required.

Run: python3 -m unittest discover -s test -t . -v
"""

from __future__ import annotations

import contextlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test.helpers import ROOT, bdcp


def make_container(
    name: str,
    *,
    running: bool = True,
    project: str = "proj",
    workdir: str = "/srv/repo/proj",
    config: str | None = None,
    service: str = "svc",
    labels: bool = True,
    binds: list[str] | None = None,
    oneoff: bool = False,
) -> dict:
    """Build a fake `docker inspect` entry for a container."""
    container = {
        "Name": f"/{name}",
        "State": {"Running": running},
        "Mounts": [{"Type": "bind", "Source": path} for path in binds or []],
        "Config": {"Labels": {}},
    }
    if labels:
        container["Config"]["Labels"] = {
            bdcp.LABEL_PROJECT: project,
            bdcp.LABEL_WORKDIR: workdir,
            bdcp.LABEL_CONFIG: config or f"{workdir}/compose.yaml",
            bdcp.LABEL_SERVICE: service,
        }
        if oneoff:
            container["Config"]["Labels"][bdcp.LABEL_ONEOFF] = "True"
    return container


class IsWithinTests(unittest.TestCase):
    """Path containment, the guard that decides what belongs to the backup."""

    def test_same_path_is_within(self) -> None:
        """A folder is inside itself."""
        self.assertTrue(bdcp.is_within(Path("/srv/repo"), Path("/srv/repo")))

    def test_child_is_within(self) -> None:
        """A nested path is inside its parent."""
        self.assertTrue(bdcp.is_within(Path("/srv/repo/app/data"), Path("/srv/repo")))

    def test_sibling_with_shared_prefix_is_not_within(self) -> None:
        """`/srv/repo2` is not inside `/srv/repo` despite the string prefix."""
        self.assertFalse(bdcp.is_within(Path("/srv/repo2"), Path("/srv/repo")))

    def test_parent_is_not_within_child(self) -> None:
        """Containment is one-directional."""
        self.assertFalse(bdcp.is_within(Path("/srv"), Path("/srv/repo")))


class BindSourcesTests(unittest.TestCase):
    """Extracting host paths from `docker inspect` mounts."""

    def test_returns_bind_sources(self) -> None:
        """Bind mounts yield their host source path."""
        container = make_container("c1", binds=["/srv/repo/a", "/srv/repo/b"])
        self.assertEqual(bdcp.bind_sources(container), [Path("/srv/repo/a"), Path("/srv/repo/b")])

    def test_ignores_named_volumes(self) -> None:
        """Named volumes have no host path and are skipped."""
        container = {"Mounts": [{"Type": "volume", "Name": "vol1"}]}
        self.assertEqual(bdcp.bind_sources(container), [])

    def test_ignores_missing_source(self) -> None:
        """A bind mount without a Source is skipped instead of crashing."""
        container = {"Mounts": [{"Type": "bind", "Source": ""}]}
        self.assertEqual(bdcp.bind_sources(container), [])

    def test_handles_no_mounts(self) -> None:
        """A container without mounts yields nothing."""
        self.assertEqual(bdcp.bind_sources({"Mounts": None}), [])


class PlanTests(unittest.TestCase):
    """Pure planning logic, no Docker involved."""

    def test_running_project_under_root_is_stopped(self) -> None:
        """A running project inside the source folder is selected."""
        containers = [make_container("c1", project="p1", service="s1")]
        projects, warnings = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(list(projects), ["p1"])
        self.assertEqual(projects["p1"].services, {"s1"})
        self.assertEqual(warnings, [])

    def test_project_outside_root_is_ignored(self) -> None:
        """A project living outside the source folder is left alone."""
        containers = [make_container("c1", project="p1", workdir="/srv/other/p1")]
        projects, _ = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(projects, {})

    def test_project_outside_root_with_inside_mount_raises(self) -> None:
        """A project outside the folder writing inside it aborts the run."""
        containers = [make_container("c1", project="p1", workdir="/srv/other/p1", binds=["/srv/repo/p1/data"])]
        with self.assertRaises(bdcp.ForeignMountError):
            bdcp.plan(containers, Path("/srv/repo"))

    def test_non_compose_container_with_inside_mount_raises(self) -> None:
        """A non-compose container writing inside the folder aborts the run."""
        containers = [make_container("c1", labels=False, binds=["/srv/repo/data"])]
        with self.assertRaises(bdcp.ForeignMountError):
            bdcp.plan(containers, Path("/srv/repo"))

    def test_non_compose_container_without_inside_mount_is_ignored(self) -> None:
        """A non-compose container writing elsewhere does not matter."""
        containers = [make_container("c1", labels=False, binds=["/srv/elsewhere"])]
        projects, _ = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(projects, {})

    def test_stopped_container_does_not_stop_project(self) -> None:
        """A project with only stopped containers is not stopped."""
        containers = [make_container("c1", project="p1", running=False)]
        projects, warnings = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(projects, {})
        self.assertEqual(len(warnings), 1)
        self.assertIn("p1", warnings[0])

    def test_services_are_deduplicated(self) -> None:
        """Two containers of one project produce one stop entry with both services."""
        containers = [
            make_container("c1", project="p1", service="s1"),
            make_container("c2", project="p1", service="s2"),
        ]
        projects, _ = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(list(projects), ["p1"])
        self.assertEqual(projects["p1"].services, {"s1", "s2"})

    def test_only_running_services_are_collected(self) -> None:
        """A stopped service of a running project is not restarted."""
        containers = [
            make_container("c1", project="p1", service="s1"),
            make_container("c2", project="p1", service="s2", running=False),
        ]
        projects, _ = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(projects["p1"].services, {"s1"})

    def test_project_with_config_inside_root_is_stopped(self) -> None:
        """A project whose workdir is outside but config file inside still counts."""
        containers = [
            make_container(
                "c1",
                project="p1",
                workdir="/srv/other",
                config="/srv/repo/p1/compose.yaml",
            )
        ]
        projects, _ = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(list(projects), ["p1"])

    def test_sibling_folder_with_shared_prefix_is_ignored(self) -> None:
        """`/srv/repo2` is not treated as part of `/srv/repo`."""
        containers = [make_container("c1", project="p1", workdir="/srv/repo2/p1")]
        projects, _ = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(projects, {})

    def test_oneoff_container_writing_inside_raises(self) -> None:
        """A `compose run` container is not stopped by `compose stop`, so it aborts the run."""
        containers = [make_container("c1", project="p1", oneoff=True, binds=["/srv/repo/p1/data"])]
        with self.assertRaises(bdcp.ForeignMountError):
            bdcp.plan(containers, Path("/srv/repo"))

    def test_oneoff_container_outside_root_is_ignored(self) -> None:
        """A one-off container writing elsewhere does not matter."""
        containers = [make_container("c1", project="p1", oneoff=True, binds=["/srv/elsewhere"])]
        projects, _ = bdcp.plan(containers, Path("/srv/repo"))
        self.assertEqual(projects, {})


class ProjectTests(unittest.TestCase):
    """Compose command construction and folder membership."""

    def test_compose_uses_single_config_file(self) -> None:
        """A single config file produces one -f flag."""
        project = bdcp.Project("p1", "/work", "/work/compose.yaml")
        self.assertEqual(
            project.compose("stop"),
            [
                "docker",
                "compose",
                "-p",
                "p1",
                "--project-directory",
                "/work",
                "-f",
                "/work/compose.yaml",
                "stop",
            ],
        )

    def test_compose_splits_multiple_config_files(self) -> None:
        """Comma-separated config files each get their own -f flag."""
        project = bdcp.Project("p1", "/work", "/work/a.yaml,/work/b.yaml")
        cmd = project.compose("up", "-d")
        self.assertEqual(cmd.count("-f"), 2)
        self.assertIn("/work/a.yaml", cmd)
        self.assertIn("/work/b.yaml", cmd)
        self.assertEqual(cmd[-2:], ["up", "-d"])

    def test_under_checks_workdir(self) -> None:
        """A project is under the root when its workdir is."""
        project = bdcp.Project("p1", "/srv/repo/p1", "/srv/repo/p1/compose.yaml")
        self.assertTrue(project.under(Path("/srv/repo")))

    def test_under_checks_config_files(self) -> None:
        """A project is under the root when only its config file is."""
        project = bdcp.Project("p1", "/srv/other", "/srv/repo/p1/compose.yaml")
        self.assertTrue(project.under(Path("/srv/repo")))

    def test_not_under_when_everything_is_outside(self) -> None:
        """A project with workdir and config outside the root is not under it."""
        project = bdcp.Project("p1", "/srv/other", "/srv/other/compose.yaml")
        self.assertFalse(project.under(Path("/srv/repo")))


class BackupTreeTests(unittest.TestCase):
    """Archive naming and the root-container tar invocation."""

    def setUp(self) -> None:
        """Give each test a real destination directory."""
        self._tmp = tempfile.TemporaryDirectory()
        self.backup_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        """Remove the temporary destination."""
        self._tmp.cleanup()

    def record(self, calls: list[list[str]]) -> object:
        """Stand-in for `run` that records the command and fakes the archive."""

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            return ""

        return fake_run

    def test_returns_timestamped_archive_name(self) -> None:
        """The archive name is <folder>__<timestamp>.tar.gz."""
        with mock.patch.object(bdcp, "run", side_effect=self.record([])):
            archive = bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "2026_01_01__00_00_00")
        self.assertEqual(archive, "repo__2026_01_01__00_00_00.tar.gz")

    def test_runs_tar_inside_alpine_as_root(self) -> None:
        """tar runs in an alpine container, which can read root-owned files."""
        calls: list[list[str]] = []
        with mock.patch.object(bdcp, "run", side_effect=self.record(calls)):
            bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "ts")
        cmd = calls[0]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("alpine", cmd)
        self.assertEqual(cmd[-3:][0], "sh")
        self.assertEqual(cmd[-3:][1], "-c")

    def test_source_is_mounted_read_only(self) -> None:
        """The source folder is mounted read-only, so archiving cannot modify it."""
        calls: list[list[str]] = []
        with mock.patch.object(bdcp, "run", side_effect=self.record(calls)):
            bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "ts")
        cmd = calls[0]
        self.assertIn("/srv/repo:/src/repo:ro", cmd)
        self.assertIn(f"{self.backup_dir}:/backup", cmd)

    def test_archive_keeps_the_folder_as_top_level_entry(self) -> None:
        """tar extracts from /src so paths start with the folder name."""
        calls: list[list[str]] = []
        with mock.patch.object(bdcp, "run", side_effect=self.record(calls)):
            bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "ts")
        script = calls[0][-1]
        self.assertIn("-C /src repo", script)

    def test_writes_to_part_file_first(self) -> None:
        """The archive is written as .part so a partial file never looks complete."""
        calls: list[list[str]] = []
        with mock.patch.object(bdcp, "run", side_effect=self.record(calls)):
            bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "ts")
        script = calls[0][-1]
        self.assertIn("/backup/repo__ts.tar.gz.part", script)
        self.assertIn("mv /backup/repo__ts.tar.gz.part /backup/repo__ts.tar.gz", script)

    def test_archive_is_chowned_to_the_invoking_user(self) -> None:
        """The archive is chowned back so the normal user can move or delete it."""
        calls: list[list[str]] = []
        with mock.patch.object(bdcp, "run", side_effect=self.record(calls)):
            bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "ts")
        script = calls[0][-1]
        self.assertIn(f"chown {os.getuid()}:{os.getgid()}", script)

    def test_part_file_is_removed_inside_the_container_on_failure(self) -> None:
        """A failing tar removes its root-owned .part inside the container.

        The invoking user could not delete a root-owned file, so cleanup has to
        happen where root can reach it.
        """
        calls: list[list[str]] = []
        with mock.patch.object(bdcp, "run", side_effect=self.record(calls)):
            bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "ts")
        script = calls[0][-1]
        self.assertIn("rm -f /backup/repo__ts.tar.gz.part", script)
        self.assertIn("exit 1", script)

    def test_folder_name_is_quoted(self) -> None:
        """A folder name with spaces does not break the shell command."""
        calls: list[list[str]] = []
        with mock.patch.object(bdcp, "run", side_effect=self.record(calls)):
            bdcp.backup_tree(Path("/srv/my repo"), self.backup_dir, "ts")
        script = calls[0][-1]
        self.assertIn("-C /src 'my repo'", script)

    def test_interrupted_archive_kills_the_container_and_cleans_up(self) -> None:
        """A SIGTERM during archiving must not leave the container writing.

        Killing the docker CLI does not stop the container, so the archive would
        keep being written while the projects are already restarted.
        """
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            raise KeyboardInterrupt

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp.subprocess, "run") as sub_mock,
            self.assertRaises(KeyboardInterrupt),
        ):
            bdcp.backup_tree(Path("/srv/repo"), self.backup_dir, "ts")

        cleanup = [c.args[0] for c in sub_mock.call_args_list]
        self.assertTrue(
            any(c[:3] == ["docker", "rm", "-f"] for c in cleanup),
            "the archive container was not killed",
        )
        self.assertTrue(
            any("rm" in c and any(".part" in a for a in c) for c in cleanup),
            "the root-owned .part file was not removed",
        )


class RunTests(unittest.TestCase):
    """The thin subprocess wrapper."""

    def test_returns_stdout_when_capturing(self) -> None:
        """capture=True returns the command's stdout."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="hello\n")
        with mock.patch.object(bdcp.subprocess, "run", return_value=completed):
            self.assertEqual(bdcp.run(["docker", "ps"], capture=True), "hello\n")

    def test_returns_empty_string_without_capture(self) -> None:
        """capture=False returns an empty string."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ignored")
        with mock.patch.object(bdcp.subprocess, "run", return_value=completed):
            self.assertEqual(bdcp.run(["docker", "ps"]), "")

    def test_raises_on_nonzero_exit(self) -> None:
        """A non-zero exit code raises RuntimeError naming the command."""
        completed = subprocess.CompletedProcess(args=[], returncode=2, stdout="")
        with mock.patch.object(bdcp.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as ctx:
                bdcp.run(["docker", "bogus"])
        self.assertIn("docker bogus", str(ctx.exception))

    def test_error_includes_stderr(self) -> None:
        """The failure message carries the command's stderr, so the cause is visible."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Cannot connect to the Docker daemon\n"
        )
        with mock.patch.object(bdcp.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as ctx:
                bdcp.run(["docker", "ps"])
        self.assertIn("Cannot connect to the Docker daemon", str(ctx.exception))

    def test_prints_command_when_verbose(self) -> None:
        """--verbose echoes the command before running it."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        out = io.StringIO()
        with (
            mock.patch.object(bdcp.subprocess, "run", return_value=completed),
            mock.patch.object(bdcp, "VERBOSE", True),
            contextlib.redirect_stdout(out),
        ):
            bdcp.run(["docker", "ps"])
        self.assertIn("$ docker ps", out.getvalue())


class MainTests(unittest.TestCase):
    """main() control flow with Docker calls mocked out."""

    def setUp(self) -> None:
        """Create a real source folder so path validation passes."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "repo"
        self.source.mkdir()
        self.backup_dir = self.tmp / "backups"

    def tearDown(self) -> None:
        """Remove the temporary tree."""
        self._tmp.cleanup()

    def run_main(self, containers: list[dict], *args: str) -> int:
        """Run main() with discovery mocked to return `containers`."""
        with mock.patch.object(bdcp, "discover", return_value=containers):
            return bdcp.main([str(self.source), str(self.backup_dir), *args])

    def test_dry_run_changes_nothing(self) -> None:
        """--dry-run runs no command and creates no backup directory."""
        containers = [make_container("c1", project="p1", workdir=str(self.source / "p1"))]
        with mock.patch.object(bdcp, "run") as run_mock:
            rc = self.run_main(containers, "--dry-run")
        self.assertEqual(rc, 0)
        run_mock.assert_not_called()
        self.assertFalse(self.backup_dir.exists())

    def test_missing_source_exits_nonzero(self) -> None:
        """A source folder that does not exist is refused."""
        with mock.patch.object(bdcp, "run") as run_mock:
            rc = bdcp.main([str(self.tmp / "nope"), str(self.backup_dir)])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()

    def test_destination_inside_source_is_refused(self) -> None:
        """A destination inside the source folder is refused before any Docker call."""
        with mock.patch.object(bdcp, "run") as run_mock:
            rc = bdcp.main([str(self.source), str(self.source / "backups")])
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()

    def test_filesystem_root_is_refused(self) -> None:
        """Archiving `/` is refused.

        Every destination is inside `/`, so the destination check catches this
        case; no separate root guard is needed.
        """
        err = io.StringIO()
        with (
            mock.patch.object(bdcp, "run") as run_mock,
            contextlib.redirect_stderr(err),
        ):
            rc = bdcp.main(["/", str(self.backup_dir)])
        self.assertEqual(rc, 1)
        self.assertIn("inside the source folder", err.getvalue())
        run_mock.assert_not_called()

    def test_foreign_mount_exits_nonzero(self) -> None:
        """A non-compose container writing inside the source aborts the run."""
        containers = [make_container("c1", labels=False, binds=[str(self.source / "data")])]
        with mock.patch.object(bdcp, "run") as run_mock:
            rc = self.run_main(containers)
        self.assertEqual(rc, 1)
        run_mock.assert_not_called()

    def test_successful_run_stops_archives_and_restarts(self) -> None:
        """A clean run stops the project, writes the archive and restarts it."""
        containers = [make_container("c1", project="p1", workdir=str(self.source / "p1"), service="s1")]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            return ""

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp, "backup_tree", return_value="repo__ts.tar.gz"),
        ):
            rc = self.run_main(containers)

        self.assertEqual(rc, 0)
        self.assertTrue(any("stop" in cmd for cmd in calls), "project was not stopped")
        self.assertTrue(any("up" in cmd for cmd in calls), "project was not restarted")
        self.assertTrue(self.backup_dir.is_dir())

    def test_archive_failure_still_restarts_project(self) -> None:
        """A failing archive still restarts the stopped project and exits 1."""
        containers = [make_container("c1", project="p1", workdir=str(self.source / "p1"), service="s1")]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            return ""

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp, "backup_tree", side_effect=RuntimeError("boom")),
        ):
            rc = self.run_main(containers)

        self.assertEqual(rc, 1)
        self.assertTrue(any("up" in cmd for cmd in calls), "project was not restarted")

    def test_stop_failure_skips_archive_and_restarts(self) -> None:
        """A failing stop skips the archive, restarts what stopped, and exits 1."""
        containers = [make_container("c1", project="p1", workdir=str(self.source / "p1"), service="s1")]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            if "stop" in cmd:
                raise RuntimeError("stop boom")
            return ""

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp, "backup_tree") as backup_mock,
        ):
            rc = self.run_main(containers)

        self.assertEqual(rc, 1)
        backup_mock.assert_not_called()
        self.assertTrue(any("up" in cmd for cmd in calls), "project was not restarted")

    def test_restart_failure_is_reported(self) -> None:
        """A failing restart is reported and the run exits 1."""
        containers = [make_container("c1", project="p1", workdir=str(self.source / "p1"), service="s1")]

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            if "up" in cmd:
                raise RuntimeError("restart boom")
            return ""

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp, "backup_tree", return_value="repo__ts.tar.gz"),
        ):
            rc = self.run_main(containers)

        self.assertEqual(rc, 1)

    def test_restart_order_is_reversed(self) -> None:
        """Projects restart in the reverse order they were stopped."""
        containers = [
            make_container("c1", project="p1", workdir=str(self.source / "p1"), service="s1"),
            make_container("c2", project="p2", workdir=str(self.source / "p2"), service="s2"),
        ]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            return ""

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp, "backup_tree", return_value="repo__ts.tar.gz"),
        ):
            rc = self.run_main(containers)

        self.assertEqual(rc, 0)
        stopped = [cmd[cmd.index("-p") + 1] for cmd in calls if "stop" in cmd]
        restarted = [cmd[cmd.index("-p") + 1] for cmd in calls if "up" in cmd]
        self.assertEqual(restarted, list(reversed(stopped)))

    def test_only_running_services_are_restarted(self) -> None:
        """A service that was already stopped is not started by the restart."""
        containers = [
            make_container("c1", project="p1", workdir=str(self.source / "p1"), service="s1"),
            make_container("c2", project="p1", workdir=str(self.source / "p1"), service="s2", running=False),
        ]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            return ""

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp, "backup_tree", return_value="repo__ts.tar.gz"),
        ):
            rc = self.run_main(containers)

        self.assertEqual(rc, 0)
        up_cmd = next(cmd for cmd in calls if "up" in cmd)
        self.assertIn("s1", up_cmd)
        self.assertNotIn("s2", up_cmd)

    def test_restart_does_not_start_dependencies(self) -> None:
        """The restart passes --no-deps so a deliberately stopped dependency stays stopped."""
        containers = [make_container("c1", project="p1", workdir=str(self.source / "p1"), service="s1")]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            return ""

        with (
            mock.patch.object(bdcp, "run", side_effect=fake_run),
            mock.patch.object(bdcp, "backup_tree", return_value="repo__ts.tar.gz"),
        ):
            rc = self.run_main(containers)

        self.assertEqual(rc, 0)
        up_cmd = next(cmd for cmd in calls if "up" in cmd)
        self.assertIn("--no-deps", up_cmd)

    def test_warning_for_project_without_running_containers(self) -> None:
        """A project with no running containers warns but does not fail."""
        containers = [make_container("c1", project="p1", workdir=str(self.source / "p1"), running=False)]
        err = io.StringIO()
        with (
            mock.patch.object(bdcp, "run"),
            mock.patch.object(bdcp, "backup_tree", return_value="repo__ts.tar.gz"),
            contextlib.redirect_stderr(err),
        ):
            rc = self.run_main(containers)
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", err.getvalue())


class MainGuardTests(unittest.TestCase):
    """The `if __name__ == "__main__"` entry point."""

    def test_main_guard_exits_with_main_status(self) -> None:
        """Running the file as a script exits with main()'s return code."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        with (
            mock.patch.object(bdcp.subprocess, "run", return_value=completed),
            tempfile.TemporaryDirectory() as tmp,
        ):
            source = Path(tmp) / "repo"
            source.mkdir()
            with mock.patch.object(
                sys, "argv", ["backup_docker_compose_projects.py", str(source), str(Path(tmp) / "out"), "--dry-run"]
            ):
                with self.assertRaises(SystemExit) as ctx:
                    runpy.run_path(str(ROOT / "backup_docker_compose_projects.py"), run_name="__main__")
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
