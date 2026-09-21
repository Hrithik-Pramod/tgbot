"""
Nothing carrying a credential is allowed near a public repository.

WHAT HAPPENED (22 September 2026)

A deploy was being chased on the production server and `git status` showed:

    A  .env.bak.2026-09-09-1841
    A  .env.bak.golive-2026-09-10-1324
    A  .env.bak.golive-2026-09-10-1335

Staged. Three snapshots of the environment file — bot tokens, database
password, TRON API key — sitting in the index of a PUBLIC repository, one
`git commit && git push` from being permanent and world-readable. They were
picked up by a `git add -A` run on the server.

WHY .gitignore DID NOT STOP IT

The rule was the bare name:

    .env

which matches a file called exactly `.env` and nothing else. `.env.bak…` is
a different filename and was never covered. The rule had looked right for
two weeks because nobody had made a backup in the repository directory
before.

WHAT THIS TEST IS

The rule is one line in a file nobody reads twice, and the cost of it being
wrong is every credential the system has. So the pattern is asserted rather
than trusted, against the actual names that turned up plus the shapes a
future backup is likely to take.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The three that were found staged, and the shapes the backup scripts and
# ordinary habit produce.
MUST_BE_IGNORED = [
    ".env.bak.2026-09-09-1841",
    ".env.bak.golive-2026-09-10-1324",
    ".env.bak.golive-2026-09-10-1335",
    ".env.bak",
    ".env.backup",
    ".env.old",
    ".env.save",
    ".env.local",
    ".env.production",
    ".env.1",
    ".env~",
]

# The one that must NOT be ignored: it is the committed template, and every
# setup instruction in the repo starts by copying it.
MUST_BE_TRACKED = ".env.example"


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return (ROOT / ".git").exists()


def _ignored(name: str) -> bool:
    """git's own answer, not a reimplementation of its matching rules."""
    r = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", name],
        cwd=ROOT, capture_output=True,
    )
    return r.returncode == 0


needs_git = pytest.mark.skipif(
    not _git_available(), reason="not a git checkout"
)


class TestEnvironmentBackupsAreIgnored:
    @needs_git
    @pytest.mark.parametrize("name", MUST_BE_IGNORED)
    def test_every_env_variant_is_ignored(self, name):
        assert _ignored(name), (
            f"{name} would be committed. This repository is public and that "
            "file carries the bot token and database password."
        )

    @needs_git
    def test_the_template_is_still_committable(self):
        """
        Over-broad ignoring is its own failure: every setup path in the repo
        begins `cp .env.example .env`, and it cannot be copied if it is not
        there.
        """
        assert not _ignored(MUST_BE_TRACKED)


class TestTheRuleIsWrittenDownNotInferred:
    def test_the_gitignore_covers_the_variants(self):
        """
        Belt and braces: if the checkout is unavailable in CI the test above
        skips, and this one still holds the line.
        """
        lines = [
            ln.strip()
            for ln in (ROOT / ".gitignore").read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert ".env*" in lines, (
            "the bare `.env` rule matches only that exact filename, and "
            "`.env.*` still misses the .env~ that nano leaves behind"
        )
        assert "!.env.example" in lines


class TestNoSecretIsAlreadyInTheTree:
    def test_no_env_backup_is_sitting_in_the_repo_directory(self):
        """
        A file that exists is a file that can be staged by the next
        `git add -A`. Ignoring it is the second line of defence; not keeping
        it here is the first.

        If this fails, move the backup out of the repository — do not
        delete it and do not merely rely on the ignore rule.
        """
        strays = [
            p.name for p in ROOT.iterdir()
            if p.is_file() and p.name.startswith(".env")
            and p.name != ".env" and p.name != MUST_BE_TRACKED
        ]
        assert not strays, (
            "environment backups in the repository directory: "
            + ", ".join(sorted(strays))
        )
