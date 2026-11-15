"""Publish Burnline to Hugging Face: data to a public dataset, the app to a Space.

Run `hf auth login` once first. Re-running is safe: an interrupted data upload
resumes, and files that are already uploaded are skipped.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

SPACE_HEADER = """---
title: Burnline
emoji: 🔥
colorFrom: red
colorTo: yellow
sdk: docker
app_port: 7860
pinned: false
---

"""


def export_app(project: Path, target: Path) -> None:
    """Copy only files git would track, so secrets and local data stay behind."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=project, check=True, capture_output=True,
    ).stdout.decode()
    for name in filter(None, listed.split("\0")):
        source = project / name
        if source.is_file():
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    readme = target / "README.md"
    body = readme.read_text(encoding="utf-8") if readme.exists() else ""
    readme.write_text(SPACE_HEADER + body, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="Folder that contains data/merged and data/catalog")
    parser.add_argument("--skip-data", action="store_true", help="Only update the app")
    parser.add_argument("--space", default="burnline", help="Space name (default: burnline)")
    parser.add_argument("--dataset", default="burnline-data", help="Dataset name (default: burnline-data)")
    args = parser.parse_args()

    api = HfApi()
    user = api.whoami()["name"]
    dataset_id = f"{user}/{args.dataset}"
    space_id = f"{user}/{args.space}"

    if not args.skip_data:
        if args.data is None:
            raise SystemExit("Pass --data <folder>, or --skip-data to update only the app.")
        folder = args.data.expanduser().resolve()
        for required in ("data/merged/hotspots", "data/catalog/merged_manifest.json"):
            if not (folder / required).exists():
                raise SystemExit(f"{folder} is missing {required}")
        api.create_repo(dataset_id, repo_type="dataset", exist_ok=True)
        print(f"Uploading data to {dataset_id}. This is the slow part...", flush=True)
        api.upload_folder(
            repo_id=dataset_id,
            repo_type="dataset",
            folder_path=folder,
            allow_patterns=["data/merged/**", "data/catalog/**"],
        )

    api.create_repo(space_id, repo_type="space", space_sdk="docker", exist_ok=True)
    api.add_space_variable(space_id, "FIRMS_DATASET_REPO", dataset_id)
    with tempfile.TemporaryDirectory() as temporary:
        export_app(Path.cwd(), Path(temporary))
        print(f"Uploading the app to {space_id}...", flush=True)
        api.upload_folder(repo_id=space_id, repo_type="space", folder_path=temporary)
    print(f"Done. Your site: https://huggingface.co/spaces/{space_id}")


if __name__ == "__main__":
    main()
