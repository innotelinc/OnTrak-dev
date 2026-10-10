"""The real Incus client, driven against a stand-in for the CLI.

`tests.helpers.FakeIncus` implements the same *method names* as the real client, so
it can never catch a command line the CLI does not accept. That is how three
separate mistakes shipped, each of which stopped the platform dead on a real host:

  * `image info`, `info` and `storage info` have no `--format` on Incus 7.4 —
    `Error: unknown flag: --format`, exit 1 — so `image_exists()` answered False for
    every image and `template build` refused every workload, pointing the operator at
    `ontrak image build`, which answers "already an image; nothing to build";
  * `incus query` refuses `--project` outright, because the project belongs in the
    path (`Error: --project cannot be used with the query command`);
  * `exec --user` takes a *numeric* uid and refuses an account name —
    `invalid argument "root" for "--user" flag: strconv.ParseUint: parsing "root":
    invalid syntax`. `root` is the platform's default Linux account, so *every*
    shell call failed and no machine ever looked ready.

The stand-in reproduces all three rules, so these tests assert behaviour rather than
argv, and putting any of them back has to fail them. The last test names each rule
directly, for whoever arrives here from a failure.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ontrak.incus import IncusClient, IncusError

STUB = r'''#!/usr/bin/env python3
"""A stand-in for `incus`, with the CLI rules the platform's calls trip over.

  * `--format` belongs to the *list* commands; `image info`, `info` and
    `storage info` answer `Error: unknown flag: --format`, exit 1.
  * `query` is a raw API call and refuses `--project`.
  * `exec --user` takes a numeric uid, and refuses an account name with
    `strconv.ParseUint`.
  * `copy --instance-only` means "the instance without its snapshots", so the CLI
    refuses it when the source is a snapshot — which is how a student's machine is
    cloned from the template's clean snapshot.
"""
import json
import os
import sys

KNOWN_ALIASES = {"images:ubuntu/24.04", "images:debian/12", "ontrak-win-base"}
UIDS = {"root": 0, "student": 1000, "ubuntu": 1000}
ROOT = {"environment": {"server_version": "7.4", "driver": "incus"}}
POOL = {
    "name": os.environ.get("ONTRAK_INCUS_STUB_POOL", "default"),
    "driver": "zfs",
    "status": "Created",
}

raw = list(sys.argv[1:])

# Recorded whole, global flags included: the invariants are about what is *asked*.
log = os.environ.get("ONTRAK_INCUS_STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(" ".join(raw) + "\n")

argv = list(raw)
project = None
while argv and argv[0] in {"--project", "--remote"}:
    if argv[0] == "--project":
        project = argv[1]
    argv = argv[2:]


def die(message):
    sys.stderr.write("Error: " + message + "\n")
    raise SystemExit(1)


def asks_for_format(args):
    return any(a == "--format" or a.startswith("--format=") for a in args)


if not argv:
    die("no subcommand")

head, rest = argv[0], argv[1:]

# The raw API: JSON by nature, and no place for a project flag.
if head == "query":
    if project is not None:
        die("--project cannot be used with the query command")
    if asks_for_format(rest):
        die("unknown flag: --format")
    path = rest[0] if rest else ""
    if path == "/1.0":
        sys.stdout.write(json.dumps(ROOT) + "\n")
        raise SystemExit(0)
    if path.endswith("/resources") and "storage-pools/" in path:
        # What a pool reports about itself, which is how `doctor` sees a host's
        # filesystem filling up without being able to see the host's filesystem.
        sys.stdout.write(json.dumps({"space": {"total": 100, "used": 91}, "inodes": {}}) + "\n")
        raise SystemExit(0)
    sys.stdout.write(json.dumps({}) + "\n")
    raise SystemExit(0)

if head == "image" and rest[:1] == ["info"]:
    if asks_for_format(rest[1:]):
        die("unknown flag: --format")
    alias = rest[1] if len(rest) > 1 else ""
    if alias in KNOWN_ALIASES:
        sys.stdout.write("Architecture: x86_64\nFingerprint: " + "a" * 64 + "\n")
        raise SystemExit(0)
    die("Failed getting image: The requested image couldn't be found")

if head == "info":
    if asks_for_format(rest):
        die("unknown flag: --format")
    sys.stdout.write("api_extensions: []\napi_version: 1.0\n")
    raise SystemExit(0)

if head == "storage" and rest[:1] == ["info"]:
    if asks_for_format(rest[1:]):
        die("unknown flag: --format")
    sys.stdout.write("driver: zfs\n")
    raise SystemExit(0)

if head == "storage" and rest[:1] == ["list"] and asks_for_format(rest):
    sys.stdout.write(json.dumps([POOL]) + "\n")
    raise SystemExit(0)

if head == "copy":
    source = rest[0] if rest else ""
    body = source.split(":", 1)[1] if ":" in source else source
    if "/" in body and any(a == "--instance-only" for a in rest[1:]):
        die("--instance-only can't be passed when the source is a snapshot")
    sys.stdout.write("")
    raise SystemExit(0)

if head == "exec":
    # `--user` takes a uid. An account name is refused, in either spelling.
    probe = list(rest)
    while "--user" in probe:
        i = probe.index("--user")
        value = probe[i + 1] if len(probe) > i + 1 else ""
        if not value.isdigit():
            die('invalid argument "%s" for "--user" flag: strconv.ParseUint: '
                'parsing "%s": invalid syntax' % (value, value))
        del probe[i:i + 2]
    for token in [a for a in rest if a.startswith("--user=")]:
        value = token.split("=", 1)[1]
        if not value.isdigit():
            die('invalid argument "%s" for "--user" flag: strconv.ParseUint: '
                'parsing "%s": invalid syntax' % (value, value))

    # `... -- id -u <name>` is how the client resolves an account name.
    if "-u" in rest and "id" in rest:
        index = rest.index("-u")
        name = rest[index + 1] if len(rest) > index + 1 else ""
        if name in UIDS:
            sys.stdout.write(str(UIDS[name]) + "\n")
            raise SystemExit(0)
        die("id: '%s': no such user" % name)

    if "ontrak-ready" in " ".join(rest):
        sys.stdout.write("ontrak-ready\n")
        raise SystemExit(0)
    sys.stdout.write("")
    raise SystemExit(0)

if asks_for_format(rest):
    sys.stdout.write("[]\n")
    raise SystemExit(0)

sys.stdout.write("")
'''


@pytest.fixture
def stub_bin(tmp_path) -> Path:
    """The stand-in, executable, for the client to run."""
    path = tmp_path / "incus"
    path.write_text(STUB, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def client(settings, stub_bin, monkeypatch) -> IncusClient:
    # The stand-in reports the pool the settings actually name, so this stays honest
    # if the default ever changes.
    monkeypatch.setenv("ONTRAK_INCUS_STUB_POOL", settings.incus.storage_pool)
    return IncusClient(settings, binary=str(stub_bin))


@pytest.fixture
def argv_log(monkeypatch, tmp_path) -> Path:
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ONTRAK_INCUS_STUB_LOG", str(log))
    return log


# --------------------------------------------------------------------------- #
# an alias that resolves
# --------------------------------------------------------------------------- #
def test_a_serverside_alias_counts_as_present(client):
    """`images:ubuntu/24.04` is not cached on a fresh host, and does not need to be.

    A container workload launches it from the image server — the path the catalog
    planner calls `container-image` — so "not in the local cache" is not the same
    question as "cannot be provisioned".
    """
    assert client.image_exists("images:ubuntu/24.04") is True


def test_the_sites_golden_image_counts_as_present(client):
    assert client.image_exists("ontrak-win-base") is True


def test_an_alias_that_resolves_nowhere_is_absent(client):
    assert client.image_exists("images:nothing/1.0") is False


def test_a_missing_binary_reads_as_absent_rather_than_raising(settings, tmp_path):
    """`ontrak doctor` asks this on hosts that may not have Incus at all."""
    client = IncusClient(settings, binary=str(tmp_path / "no-such-incus"))
    assert client.image_exists("images:ubuntu/24.04") is False


# --------------------------------------------------------------------------- #
# the calls that asked the API for a flag it does not have
# --------------------------------------------------------------------------- #
def test_server_info_reports_the_version(client):
    """This answered `{}` on Incus 7.4, so `doctor` printed "server unknown"."""
    assert client.server_info()["environment"]["server_version"] == "7.4"


def test_storage_info_reports_the_pool_driver(client):
    """The driver decides whether clones are cheap, so an empty answer is a lie."""
    assert client.storage_info("default")["driver"] == "zfs"


def test_storage_info_defaults_to_the_configured_pool(client, settings):
    assert client.storage_info()["name"] == settings.incus.storage_pool


def test_storage_info_says_nothing_about_a_pool_that_is_not_there(client):
    assert client.storage_info("nosuchpool") == {}


def test_pool_resources_reports_the_space_a_pool_has(client):
    """`doctor` reads a host's capacity this way, from wherever it runs.

    `storage info` prints a paragraph and takes no --format, and `storage list`
    carries no usage at all, so this is the only JSON answer to "how full is the
    pool" — the one that decides whether the portal's database survives the night.
    """
    assert client.pool_resources("default")["space"] == {"total": 100, "used": 91}


def test_pool_resources_asks_the_raw_api_without_a_project(client):
    """`query` refuses --project; the stand-in enforces that, so a flag fails here."""
    assert client.pool_resources()["space"]["used"] == 91


# --------------------------------------------------------------------------- #
# the account a shell runs as
# --------------------------------------------------------------------------- #
def test_the_default_root_account_sends_no_user_flag(client, argv_log):
    """Root is what `exec` does anyway, and the name is not a valid argument."""
    client.exec_in("probe", ["/bin/true"], user="root", check=False)
    asked = argv_log.read_text(encoding="utf-8")
    assert "--user" not in asked, asked


def test_an_account_name_is_resolved_to_its_uid(client, argv_log):
    client.exec_in("probe", ["/bin/true"], user="student", check=False)
    asked = argv_log.read_text(encoding="utf-8").strip().splitlines()
    assert any("--user 1000" in line for line in asked), asked


def test_the_lookup_is_paid_once_per_instance(client, argv_log):
    for _ in range(3):
        client.exec_in("probe", ["/bin/true"], user="student", check=False)
    lookups = [line for line in argv_log.read_text(encoding="utf-8").splitlines() if "id -u" in line]
    assert len(lookups) == 1, lookups


def test_an_account_the_guest_does_not_have_is_refused(client):
    """Better a clear failure than a graded check run as the wrong account."""
    with pytest.raises(IncusError, match="no account 'ghost'"):
        client.exec_in("probe", ["/bin/true"], user="ghost", check=False)


def test_no_user_flag_is_sent_when_none_was_configured(client, argv_log):
    client.exec_in("probe", ["/bin/true"], check=False)
    assert "--user" not in argv_log.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# cloning the clean snapshot
# --------------------------------------------------------------------------- #
def test_cloning_the_clean_snapshot_works(client, argv_log):
    """Handing a student their machine: `copy tpl-x/clean <name>`.

    This is the call that fills the pool and starts every session, and the flag it
    used to carry made it fail outright.
    """
    client.copy_instance("tpl-linux-user-lifecycle-ubuntu-24-04/clean", "ontrak-sess-42")
    asked = argv_log.read_text(encoding="utf-8")
    assert "copy tpl-linux-user-lifecycle-ubuntu-24-04/clean ontrak-sess-42" in asked, asked
    assert "--instance-only" not in asked, asked


def test_copying_a_whole_instance_still_skips_its_snapshots(client, argv_log):
    """The flag's actual meaning, kept where it is valid."""
    client.copy_instance("tpl-linux-user-lifecycle-ubuntu-24-04", "ontrak-copy")
    assert "--instance-only" in argv_log.read_text(encoding="utf-8")


def test_a_cluster_qualified_snapshot_reads_the_same_way(client, argv_log):
    client.copy_instance("lab:tpl-x/clean", "ontrak-copy")
    assert "--instance-only" not in argv_log.read_text(encoding="utf-8")


def test_a_snapshot_copy_is_not_asked_to_skip_snapshots(client):
    """Fails loudly if the flag comes back on a snapshot source."""
    client.copy_instance("tpl-x/clean", "ontrak-copy")  # no IncusError


def test_the_source_of_a_copy_is_never_an_image_alias(client, argv_log):
    """A reminder in a test rather than a comment: `copy` here is always a template."""
    client.copy_instance("tpl-x/clean", "ontrak-copy")
    assert "copy tpl-x/clean" in argv_log.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the invariants, stated directly
# --------------------------------------------------------------------------- #
def test_the_client_asks_nothing_the_cli_refuses(client, argv_log):
    client.image_exists("images:ubuntu/24.04")
    client.server_info()
    client.storage_info("default")
    client.exec_in("probe", ["/bin/true"], user="root", check=False)
    client.copy_instance("tpl-x/clean", "ontrak-probe")

    asked = [line for line in argv_log.read_text(encoding="utf-8").splitlines() if line]
    assert asked, "the stand-in recorded no commands"
    for line in asked:
        if line.startswith("query"):
            assert "--project" not in line, f"query refuses a project flag: {line}"
            assert "--format" not in line, f"query has no --format: {line}"
        if line.startswith(("image info", "info ", "storage info")):
            assert "--format" not in line, f"no such flag on this subcommand: {line}"
        if line.startswith("exec"):
            assert "--user root" not in line, f"--user wants a uid, not a name: {line}"
            assert "--user student" not in line, f"--user wants a uid, not a name: {line}"
        if line.startswith("copy"):
            source = line.split()[1]
            body = source.split(":", 1)[1] if ":" in source else source
            assert not ("/" in body and "--instance-only" in line), (
                f"a snapshot source cannot skip its own snapshots: {line}"
            )
