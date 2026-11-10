# backup-docker-compose-projects

Back up a folder of Docker Compose projects to a single compressed archive, stopping the
projects first so the data they bind-mount is consistent.

## What it does

1. Lists all containers and inspects them.
2. Selects every running compose project whose working directory or config file lives
   inside the source folder.
3. Stops each selected project (`docker compose stop`).
4. Archives the whole source folder to `<destination>/<folder>__<timestamp>.tar.gz`.
5. Restarts the stopped projects with `docker compose up -d --no-recreate --no-deps`, naming
   only the services that were running, so a service you had deliberately stopped stays
   stopped — even when it is a `depends_on` dependency of a restarted service.

The archive keeps the folder name as its top-level entry, so `~/Repository` produces
`Repository__2026_11_10__12_00_00.tar.gz` containing `Repository/...`.

## Requirements

- Python 3.12 or newer (standard library only, no dependencies)
- Docker with the Compose v2 plugin (`docker compose`)
- The `alpine` image (pulled automatically if not present)

## Usage

```bash
./backup_docker_compose_projects.py <source-folder> <backup-destination-path> [options]
```

**Example:**

```bash
./backup_docker_compose_projects.py ~/Repository /mnt/backups/compose
```

The destination directory is created automatically if it does not exist.

## File ownership

Bind-mounted data is usually written by containers running as **root**, so files inside the
source folder can be mode `600` and unreadable by your normal user. A host `tar` run as
uid 1000 fails on them:

```
tar: repo/project-a/data/marker.txt: Cannot open: Permission denied
```

The script therefore runs `tar` **inside an `alpine` container as root**:

1. The source folder is mounted **read-only** at `/src/<folder>` — archiving cannot modify it.
2. The destination is mounted at `/backup`.
3. `tar` writes `<folder>__<timestamp>.tar.gz.part`, then the container `chown`s it to
   **your** uid/gid (`os.getuid()`/`os.getgid()`, not a hardcoded `1000:1000`) and renames it
   to the final name, so you can move or delete the archive afterwards.
4. If `tar` fails, the container removes its own root-owned `.part` file — the normal user
   could not delete it.

The resulting archive keeps the original ownership and permissions inside the tarball, so a
restore reproduces them.

## Options

| Option | Description |
|---|---|
| `--dry-run` | Print the plan and exit without touching anything. |
| `--verbose`, `-v` | Echo every command before running it. |

**Examples:**

```bash
# preview what would be stopped and archived
./backup_docker_compose_projects.py ~/Repository /mnt/backups/compose --dry-run

# real run, echoing every docker and tar command
./backup_docker_compose_projects.py ~/Repository /mnt/backups/compose -v
```

## Safety behaviour

- **The destination must be outside the source folder.** Otherwise the archive would try to
  contain itself. The run exits 1 with
  `backup destination <path> is inside the source folder <path>`.
- **Archiving the filesystem root is refused.**
- **A running container that writes inside the source folder but cannot be stopped aborts
  the run** with exit code 1. That covers a container with no compose labels, a compose
  project whose working directory lives outside the source folder, and a one-off
  `docker compose run` container (which `docker compose stop` does not stop). Backing up a
  folder that something is still writing to would produce a torn archive, so the script
  refuses instead.
- **A project with no running containers only warns.** There is nothing to stop, and its
  files are still archived.
- If a stop fails, the archive is skipped, the projects stopped so far are restarted, and
  the run exits 1.
- If the archive fails, the stopped projects are still restarted (the restart runs in a
  `finally` block) and the script exits 1. The same applies to `SIGTERM`
  (`systemctl stop`, host shutdown, `kill`).
- **An interrupted archive does not leave a container running.** Killing the `docker`
  command does not stop the container it started, so on `SIGTERM` the script kills the
  archive container by name and removes the root-owned `.part` file it may have left.
  Without that, the container would keep writing the archive while the projects were
  already restarted.
- Projects are restarted in the reverse order they were stopped.
- The archive is written as `*.tar.gz.part` and renamed only on success, so a failed or
  partial archive never appears under its final name. Both the cleanup of a failed `.part`
  and the rename happen inside the root container, because the file it just created is
  root-owned. The rename is atomic because both files live in the destination directory.

## Scope

This script archives the project folders only. Named Docker volumes are **not** included —
use [backup-docker-volumes](../backup-docker-volumes) for those. A complete backup of a
self-hosted stack usually runs both.

## Tests

The Python script has a test suite using the standard library `unittest` — no extra
dependencies.

```bash
python3 -m unittest discover -s test -t . -v   # all tests
python3 -m unittest test.test_unit             # unit tests only, no Docker needed
python3 -m unittest test.test_integration      # real Docker, can take a few minutes
```

| File | Contents |
|---|---|
| `test/helpers.py` | `sys.path` bootstrap shared by both suites |
| `test/test_unit.py` | Mocked tests for path containment, planning, archiving and failure handling |
| `test/test_integration.py` | End-to-end runs against the fixture compose projects |
| `test/fixtures/project-a` | One service bind-mounting `./data` and writing a root-owned, mode-600 file |
| `test/fixtures/project-b` | Two services bind-mounting `./data/one` and `./data/two` |
| `test/fixtures/outside` | A project outside the source folder that writes inside it |

Integration tests build a throwaway source folder in a temporary directory, copy the
fixtures into it, start them, run the script, verify the archive contents and that every
container is running again, then remove everything. They never touch `~/Repository`. They
are skipped when Docker is unavailable. Do not run them at the same time as a real backup
on the same host.

`project-a` writes its marker file as `root` with mode `600`, so the tests prove the
ownership handling: a `test_fixture_really_writes_root_owned_unreadable_file` case asserts
the normal user gets `PermissionError` reading it, and the archive is then verified to
contain that file and to be owned by the invoking user. Teardown also runs inside a root
container, since the invoking user cannot delete those files.
