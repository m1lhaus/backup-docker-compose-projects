#!/usr/bin/env python3
"""Back up a folder of Docker Compose projects, stopping them for a consistent archive.

Usage: ./backup_docker_compose_projects.py <source-folder> <backup-destination-path> [options]

Every compose project whose working directory or config file lives inside the
source folder is stopped, the whole folder is archived to a single .tar.gz, and
the stopped projects are brought back up.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LABEL_PROJECT = "com.docker.compose.project"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG = "com.docker.compose.project.config_files"
LABEL_SERVICE = "com.docker.compose.service"
LABEL_ONEOFF = "com.docker.compose.oneoff"

VERBOSE = False


class ForeignMountError(RuntimeError):
    """A running container writes inside the source folder but cannot be stopped."""


def run(cmd: list[str], *, capture: bool = False) -> str:
    if VERBOSE:
        print("    $", " ".join(cmd))
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}" + (f"\n{detail}" if detail else ""))
    return result.stdout if capture else ""


def is_within(path: Path, root: Path) -> bool:
    """True when `path` is `root` or lives below it.

    Compares resolved paths instead of string prefixes, so `/srv/repo2` is not
    mistaken for a child of `/srv/repo`.
    """
    path, root = path.resolve(), root.resolve()
    return path == root or root in path.parents


def bind_sources(container: dict) -> list[Path]:
    """Host paths bind-mounted into a container."""
    return [
        Path(mount["Source"])
        for mount in container.get("Mounts") or []
        if mount.get("Type") == "bind" and mount.get("Source")
    ]


@dataclass
class Project:
    name: str
    workdir: str
    config_files: str
    services: set[str] = field(default_factory=set)

    def compose(self, *args: str) -> list[str]:
        cmd = ["docker", "compose", "-p", self.name, "--project-directory", self.workdir]
        for path in self.config_files.split(","):
            cmd += ["-f", path]
        return cmd + list(args)

    def under(self, root: Path) -> bool:
        """True when the project's working directory or config file is inside `root`."""
        raw = [self.workdir, *self.config_files.split(",")]
        return any(is_within(Path(path), root) for path in raw if path)


def discover() -> list[dict]:
    names = run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture=True).split()
    return json.loads(run(["docker", "inspect", *names], capture=True)) if names else []


def plan(containers: list[dict], source_root: Path) -> tuple[dict[str, Project], list[str]]:
    """Return the running projects to stop and any warnings.

    Every running compose project under `source_root` is stopped, whether or not
    its bind mounts are visible. A running container that writes inside the
    source folder but cannot be stopped (no compose labels, or a project living
    outside the folder) raises ForeignMountError instead of risking a torn
    archive.
    """
    projects: dict[str, Project] = {}
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        name = container["Name"].lstrip("/")
        running = container["State"]["Running"]
        inside = [path for path in bind_sources(container) if is_within(path, source_root)]

        if inside and running and LABEL_PROJECT not in labels:
            raise ForeignMountError(
                f"container '{name}' bind-mounts {inside[0]} inside {source_root} "
                "but is not managed by a compose project"
            )
        if LABEL_PROJECT not in labels:
            continue
        if labels.get(LABEL_ONEOFF) == "True":
            # `docker compose stop` ignores one-off `run` containers, so they
            # would keep writing into the folder while it is archived.
            if inside and running:
                raise ForeignMountError(
                    f"container '{name}' is a one-off 'docker compose run' container "
                    f"bind-mounting {inside[0]} inside {source_root}; "
                    "stop it before backing up"
                )
            continue

        project = Project(labels[LABEL_PROJECT], labels[LABEL_WORKDIR], labels[LABEL_CONFIG])
        if not project.under(source_root):
            if inside and running:
                raise ForeignMountError(
                    f"container '{name}' bind-mounts {inside[0]} inside {source_root} "
                    f"but its compose project '{project.name}' lives outside it"
                )
            continue

        project = projects.setdefault(project.name, project)  # reduce duplicates
        if running:
            project.services.add(labels[LABEL_SERVICE])

    warnings = [
        f"project '{project.name}' has no running containers, nothing to stop"
        for project in projects.values()
        if not project.services
    ]
    return {name: p for name, p in projects.items() if p.services}, warnings


def backup_tree(source_root: Path, backup_dir: Path, timestamp: str) -> str:
    """Archive the whole source folder from inside a root container.

    Bind-mounted data is often written by containers running as root, so files
    can be mode 600 and unreadable by the invoking user. tar therefore runs in an
    alpine container as root, and the finished archive is chowned back to the
    invoking user so it can be moved or deleted afterwards.

    The source is mounted read-only at /src/<folder>, so the archive keeps the
    folder name as its top-level entry. The archive is written as .part and
    renamed only on success, so a partial file never appears under its final name.
    Cleanup on failure also happens inside the container, because the .part file
    is root-owned and the invoking user could not delete it.
    """
    archive = f"{source_root.name}__{timestamp}.tar.gz"
    name = shlex.quote(source_root.name)
    part = shlex.quote(f"/backup/{archive}.part")
    final = shlex.quote(f"/backup/{archive}")
    script = (
        f"tar czf {part} -C /src {name}"
        f" || {{ rm -f {part}; exit 1; }}"
        f" && chown {os.getuid()}:{os.getgid()} {part}"
        f" && mv {part} {final}"
    )
    container = f"bdcp-archive-{os.getpid()}"
    try:
        run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                container,
                "-v",
                f"{source_root}:/src/{source_root.name}:ro",
                "-v",
                f"{backup_dir}:/backup",
                "alpine",
                "sh",
                "-c",
                script,
            ]
        )
    except BaseException:
        # Killing the docker CLI does not stop the container: it keeps writing
        # the archive while the projects are already restarted. Kill it, then
        # remove the root-owned .part it may have left, which we cannot delete.
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{backup_dir}:/backup",
                "alpine",
                "rm",
                "-f",
                f"/backup/{archive}.part",
            ],
            check=False,
            capture_output=True,
        )
        raise
    return archive


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # type: ignore
    parser.add_argument("source_root", type=Path, help="folder holding the compose projects")
    parser.add_argument("backup_dir", type=Path, help="destination directory for the archive")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="echo every command")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    source_root = args.source_root.expanduser().resolve()
    backup_dir = args.backup_dir.expanduser().resolve()

    if not source_root.is_dir():
        print(f"ERROR: source folder does not exist: {source_root}", file=sys.stderr)
        return 1
    if is_within(backup_dir, source_root):
        print(f"ERROR: backup destination {backup_dir} is inside the source folder {source_root}", file=sys.stderr)
        return 1

    try:
        projects, warnings = plan(discover(), source_root)
    except ForeignMountError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)

    print(f"==> {len(projects)} compose project(s) to stop, archiving {source_root}")
    for project in projects.values():
        print(f"    stop {project.name} (services: {', '.join(sorted(project.services))})")

    if args.dry_run:
        print("==> Dry run, nothing changed.")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y_%m_%d__%H_%M_%S")
    failures: list[str] = []
    stopped: list[Project] = []
    try:
        for project in projects.values():
            stopped.append(project)  # restart it even if stopping fails partway
            print(f"==> Stopping {project.name}...")
            try:
                run(project.compose("stop", *sorted(project.services)))
            except RuntimeError as exc:
                print(f"    FAILED: {exc}", file=sys.stderr)
                failures.append(project.name)
                break  # a torn stop means no archive; restart what we stopped

        if not failures:
            print(f"==> Archiving {source_root}...")
            try:
                archive = backup_tree(source_root, backup_dir, timestamp)
                print(f"    Done: {backup_dir / archive}")
            except RuntimeError as exc:
                print(f"    FAILED: {exc}", file=sys.stderr)
                failures.append(str(source_root))
    finally:
        for project in reversed(stopped):
            print(f"==> Restarting {project.name}...")
            try:
                # --no-deps: `up` would otherwise also start services that were
                # deliberately stopped, as long as they are dependencies.
                run(project.compose("up", "-d", "--no-recreate", "--no-deps", *sorted(project.services)))
            except RuntimeError as exc:
                print(f"    FAILED to restart {project.name}: {exc}", file=sys.stderr)
                failures.append(project.name)

    if failures:
        print(f"==> Finished with {len(failures)} failure(s): {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"==> Backup complete. Archive written to: {backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
