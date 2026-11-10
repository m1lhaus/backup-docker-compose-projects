#!/usr/bin/env python3
"""Integration tests for backup_docker_compose_projects.py against real Docker.

Every test builds its own throwaway source folder in a temporary directory and
starts the fixture compose projects from there, so the real ~/Repository is
never touched. Skipped when Docker is unavailable.

Run: python3 -m unittest discover -s test -t . -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from test.helpers import ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = ROOT / "backup_docker_compose_projects.py"

# container name -> expected content of its marker.txt
EXPECTED = {
    "test-project-a-writer": "project-a",
    "test-project-b-writer-one": "project-b-one",
    "test-project-b-writer-two": "project-b-two",
}


def docker_available() -> bool:
    """True when a working Docker daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def compose(compose_file: Path, *args: str, env: dict[str, str] | None = None) -> None:
    """Run `docker compose` against one compose file."""
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )


def running_containers() -> set[str]:
    """Names of all running containers."""
    result = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True)
    return set(result.stdout.split())


def started_at(container: str) -> str:
    """The container's start timestamp, used to prove it was not restarted."""
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", container],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def archive_contents(path: Path) -> dict[str, str]:
    """Map every regular file inside a .tar.gz to its decoded content."""
    with tarfile.open(path, "r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }


@unittest.skipUnless(docker_available(), "Docker is not available")
class IntegrationTests(unittest.TestCase):
    """End-to-end runs against fixture compose projects in a temporary folder."""

    def setUp(self) -> None:
        """Create a temporary source folder holding copies of the fixtures."""
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.source = self.tmp / "repo"
        self.backup_dir = self.tmp / "backups"
        self.source.mkdir()

        for fixture in ("project-a", "project-b"):
            shutil.copytree(FIXTURES / fixture, self.source / fixture)
        # Pre-create the bind-mount targets so they are owned by this user and
        # the temporary tree can be removed afterwards.
        for path in ("project-a/data", "project-b/data/one", "project-b/data/two"):
            (self.source / path).mkdir(parents=True, exist_ok=True)

        self.started: list[Path] = []
        for fixture in ("project-a", "project-b"):
            self.start(fixture)

    def tearDown(self) -> None:
        """Stop every project this test started and remove the temporary tree.

        The fixture projects deliberately write root-owned, mode-600 files, which
        the invoking user cannot delete, so the tree is removed from inside a
        root container as well.
        """
        for compose_file in reversed(self.started):
            subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "down", "-v", "--remove-orphans"],
                capture_output=True,
                env={**os.environ, "OUTSIDE_TARGET": str(self.source / "outside-data")},
            )
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{self.tmp}:/t",
                "alpine",
                "sh",
                "-c",
                "rm -rf /t/* /t/.[!.]* 2>/dev/null; true",
            ],
            capture_output=True,
        )
        self._tmp.cleanup()

    def start(self, fixture: str, env: dict[str, str] | None = None) -> Path:
        """Start a fixture project and wait until its marker files are written."""
        compose_file = (
            self.source / fixture / "compose.yaml"
            if (self.source / fixture).exists()
            else FIXTURES / fixture / "compose.yaml"
        )
        compose(compose_file, "up", "-d", env=env)
        self.started.append(compose_file)
        return compose_file

    def wait_for_markers(self) -> None:
        """Wait until every fixture container has written its marker file."""
        deadline = time.time() + 60
        for container, expected in EXPECTED.items():
            while True:
                result = subprocess.run(
                    ["docker", "exec", container, "cat", "/data/marker.txt"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and result.stdout.strip() == expected:
                    break
                if time.time() > deadline:
                    raise AssertionError(f"fixture container {container} never wrote its marker")
                time.sleep(0.5)

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the backup script against this test's source and backup folders."""
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source), str(self.backup_dir), *args],
            capture_output=True,
            text=True,
        )

    def archives(self) -> list[Path]:
        """Archives written so far, sorted."""
        if not self.backup_dir.exists():
            return []
        return sorted(self.backup_dir.glob("*.tar.gz"))

    def test_archive_contains_every_project(self) -> None:
        """One archive holds the whole source folder, including both projects."""
        self.wait_for_markers()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

        archives = self.archives()
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].name.startswith("repo__"))
        self.assertTrue(archives[0].name.endswith(".tar.gz"))

        contents = archive_contents(archives[0])
        self.assertIn("repo/project-a/compose.yaml", contents)
        self.assertIn("repo/project-b/compose.yaml", contents)
        self.assertEqual(contents["repo/project-a/data/marker.txt"].strip(), "project-a")
        self.assertEqual(contents["repo/project-b/data/one/marker.txt"].strip(), "project-b-one")
        self.assertEqual(contents["repo/project-b/data/two/marker.txt"].strip(), "project-b-two")

    def test_fixture_really_writes_root_owned_unreadable_file(self) -> None:
        """The fixture must create a file the normal user cannot read.

        Without this the ownership test below could pass for the wrong reason.
        """
        self.wait_for_markers()
        marker = self.source / "project-a/data/marker.txt"
        self.assertEqual(marker.stat().st_uid, 0, "marker is not owned by root")
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600, "marker is not mode 600")
        with self.assertRaises(PermissionError):
            marker.read_text()

    def test_root_owned_files_end_up_in_the_archive(self) -> None:
        """A root-owned mode-600 file is archived despite being unreadable to us."""
        self.wait_for_markers()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

        contents = archive_contents(self.archives()[0])
        self.assertEqual(contents["repo/project-a/data/marker.txt"].strip(), "project-a")

    def test_archive_is_owned_by_the_invoking_user(self) -> None:
        """The archive is chowned back so the normal user can move or delete it."""
        self.wait_for_markers()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

        archive = self.archives()[0]
        self.assertEqual(archive.stat().st_uid, os.getuid())
        self.assertEqual(archive.stat().st_gid, os.getgid())
        self.assertTrue(archive.stat().st_mode & 0o200, "archive is not writable by us")
        # Prove it: deleting it must work as the normal user.
        archive.unlink()

    def test_source_folder_is_untouched(self) -> None:
        """Archiving mounts the source read-only, so files keep their mode and owner."""
        self.wait_for_markers()
        marker = self.source / "project-a/data/marker.txt"
        before = (marker.stat().st_uid, marker.stat().st_mode)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((marker.stat().st_uid, marker.stat().st_mode), before)

    def test_containers_are_running_after_backup(self) -> None:
        """Every fixture container is running again once the backup finishes."""
        self.wait_for_markers()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(set(EXPECTED) <= running_containers())

    def test_no_part_file_is_left_behind(self) -> None:
        """A successful run leaves only the final archive, no .part file."""
        self.wait_for_markers()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.backup_dir.glob("*.part")), [])

    def test_dry_run_writes_nothing(self) -> None:
        """--dry-run creates no archive and leaves the containers running."""
        self.wait_for_markers()
        before = {name: started_at(name) for name in EXPECTED}
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Dry run", result.stdout)
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual({name: started_at(name) for name in EXPECTED}, before)

    def test_destination_inside_source_is_refused(self) -> None:
        """A destination inside the source folder exits 1 and writes nothing."""
        self.wait_for_markers()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source), str(self.source / "backups")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("inside the source folder", result.stderr)
        self.assertFalse((self.source / "backups").exists())

    def test_project_outside_source_writing_inside_aborts(self) -> None:
        """A project outside the folder that writes inside it aborts the run."""
        self.wait_for_markers()
        target = self.source / "outside-data"
        target.mkdir(exist_ok=True)
        self.start("outside", env={"OUTSIDE_TARGET": str(target)})
        before = {name: started_at(name) for name in EXPECTED}

        result = self.run_script()

        self.assertEqual(result.returncode, 1)
        self.assertIn("lives outside", result.stderr)
        self.assertEqual(self.archives(), [])
        # Nothing was stopped, so nothing was restarted either.
        self.assertEqual({name: started_at(name) for name in EXPECTED}, before)

    def test_non_compose_container_writing_inside_aborts(self) -> None:
        """A non-compose container bind-mounting inside the folder aborts the run."""
        self.wait_for_markers()
        target = self.source / "foreign-data"
        target.mkdir(exist_ok=True)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                "test-foreign-writer",
                "-v",
                f"{target}:/data",
                "alpine",
                "sleep",
                "infinity",
            ],
            check=True,
            capture_output=True,
        )
        try:
            result = self.run_script()
        finally:
            subprocess.run(["docker", "rm", "-f", "test-foreign-writer"], capture_output=True)

        self.assertEqual(result.returncode, 1)
        self.assertIn("not managed by a compose project", result.stderr)
        self.assertEqual(self.archives(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
