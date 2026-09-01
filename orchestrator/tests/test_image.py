"""The Dockerfile and the workspace have to agree about what exists.

`make check` does not build the image — there is no docker daemon in CI, and
building one would turn a twenty-second gate into a five-minute one. That gap
is real and this is the cheapest thing that closes the part of it that bites:
the Dockerfile names every workspace member by hand, twice, so adding or
removing one leaves it stale.

It went wrong exactly that way. `supplier-sim` was deleted from the workspace,
every Python check stayed green, and the failure surfaced five minutes into a
Cloud Build as `COPY failed: stat supplier-sim/pyproject.toml: file does not
exist` — after the deploy had already granted IAM and pushed a service account
around. A string comparison would have caught it before the push.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

# `web` is a member of the npm workspace, not the uv one, and is excluded from
# the image on purpose.
_COPY_MANIFEST = re.compile(r"^COPY\s+([\w-]+)/pyproject\.toml\s", re.MULTILINE)


def _members() -> set[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    members = config["tool"]["uv"]["workspace"]["members"]
    assert isinstance(members, list)
    return {str(m) for m in members}


def test_the_image_copies_a_manifest_for_every_workspace_member() -> None:
    """uv parses every member's manifest to resolve the lockfile, so a member
    whose manifest is not in the build context fails `uv sync --frozen`."""
    copied = set(_COPY_MANIFEST.findall(DOCKERFILE.read_text()))

    assert copied == _members(), (
        "Dockerfile COPY lines and the uv workspace members disagree. "
        f"Only in the Dockerfile: {sorted(copied - _members())}. "
        f"Only in pyproject.toml: {sorted(_members() - copied)}."
    )


def test_every_manifest_the_image_copies_exists() -> None:
    """The failure as Cloud Build reports it, checked locally in a millisecond
    instead of after a five-minute image build and half a deploy."""
    for member in _COPY_MANIFEST.findall(DOCKERFILE.read_text()):
        assert (REPO_ROOT / member / "pyproject.toml").is_file(), member


def test_nothing_ignores_a_path_the_dockerfile_needs() -> None:
    """A .dockerignore rule excluding a copied manifest produces the same error
    as a missing file, and is harder to see: the file is right there."""
    ignored = {
        line.rstrip("/")
        for line in (REPO_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    }

    assert not ignored & _members(), sorted(ignored & _members())
