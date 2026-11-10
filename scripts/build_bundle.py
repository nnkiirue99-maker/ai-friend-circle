"""Build a source-only onboarding bundle with an explicit allowlist. No states/keys/native app.

The bundle is self-hosting: it contains this script, so a node installed from the bundle can
regenerate ``reference.zip`` and ``bundle.json`` and serve them to the next invited node.
The generated assets themselves are never packed (that would recurse), which is why every
fresh install must run this script once. Output is deterministic: same sources, same SHA-256.
"""

from pathlib import Path
import hashlib
import json
import zipfile

PROFILE = "rue-federation-dev/0.3"
SOURCE_DIRS = [
    "src/friend_circle",
    "tests",
    "adapters",
    "sdk",
    "scripts",
    "deploy",
    "native",
    "docs",
]
SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".md",
    ".sh",
    ".service",
    ".conf",
    ".html",
    ".css",
    ".svg",
    ".swift",
}
TOP_LEVEL_FILES = [
    "README.md",
    "LICENSE",
    "quickstart.sh",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
]
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def collect(root: Path) -> list[Path]:
    allowed = []
    for top in SOURCE_DIRS:
        for path in (root / top).rglob("*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix in SOURCE_SUFFIXES
                and "__pycache__" not in path.parts
            ):
                allowed.append(path)
    for name in TOP_LEVEL_FILES:
        if (root / name).is_file():
            allowed.append(root / name)
    return sorted(allowed)


def build(root: Path) -> dict:
    resources = root / "src/friend_circle/resources"
    target = resources / "reference.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in collect(root):
            info = zipfile.ZipInfo(
                "ai-friend-circle/" + path.relative_to(root).as_posix(), FIXED_TIME
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    manifest = {
        "version": 1,
        "profile": PROFILE,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "bytes": target.stat().st_size,
    }
    (resources / "bundle.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"bundle": str(target), **manifest}


if __name__ == "__main__":
    print(json.dumps(build(Path(__file__).resolve().parents[1])))
