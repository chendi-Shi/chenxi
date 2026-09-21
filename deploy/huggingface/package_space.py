"""Build an allowlisted HF deployment directory. Never copy runtime or credential files."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def package(output: Path):
    root = Path(__file__).resolve().parents[2]
    if output.exists() and any(output.iterdir()):
        raise ValueError("output_must_be_empty")
    selected = {
        "README.md": root / "deploy/huggingface/README.md",
        "Dockerfile": root / "Dockerfile",
        "pyproject.toml": root / "pyproject.toml",
        "requirements-dev.lock": root / "requirements-dev.lock",
    }
    for source in (root / "src/research_agent").rglob("*"):
        if source.is_file() and source.suffix in {".py", ".html"}:
            selected[source.relative_to(root).as_posix()] = source
    for destination, source in selected.items():
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError("unexpected_source_path")
        target = output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return sorted(selected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for filename in package(args.out):
        print(filename)
