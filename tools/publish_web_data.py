"""Upload the browser data products to a public Hugging Face dataset.

Run `hf auth login` once first, then:

    python -m tools.build_web_data
    python -m tools.publish_web_data

Re-running is safe: an interrupted upload resumes and unchanged files are
skipped. The last line printed is the address Burnline needs as its dataUrl.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from huggingface_hub import HfApi

CARD = """---
pretty_name: Burnline fire activity (NASA FIRMS)
viewer: false
tags:
- nasa-firms
- wildfire
- modis
- viirs
---

# Burnline web data

NASA FIRMS MODIS and VIIRS active-fire detections, re-sorted by location and
pre-aggregated so the Burnline explorer can query them from a browser with
DuckDB-WASM. Built by `tools/build_web_data.py`; the docstring of that script
describes every file. No observation is generated or altered.

Source: NASA Fire Information for Resource Management System (FIRMS),
https://firms.modaps.eosdis.nasa.gov. We acknowledge the use of data from
NASA's FIRMS, part of NASA's Earth Science Data and Information System (ESDIS).
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=Path("data/web"), help="Folder written by build_web_data (default: data/web)")
    parser.add_argument("--dataset", default="burnline-web", help="Dataset name (default: burnline-web)")
    args = parser.parse_args()

    folder = args.data.expanduser().resolve()
    if not (folder / "meta.json").exists():
        raise SystemExit(f"{folder} has no meta.json. Run `python -m tools.build_web_data` first.")
    leftovers = list(folder.rglob("*.partial"))
    if leftovers:
        raise SystemExit(f"{folder} has unfinished files, for example {leftovers[0]}. Re-run the build.")
    card = folder / "README.md"
    if not card.exists():
        card.write_text(CARD, encoding="utf-8")

    api = HfApi()
    repo_id = f"{api.whoami()['name']}/{args.dataset}"
    api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
    size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file()) / 1e9
    print(f"Uploading {size:.1f} GB to {repo_id}. This is the slow part...", flush=True)
    # upload_folder splits a big folder into several commits and resumes when re-run.
    api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=folder, ignore_patterns=["*.partial"])
    print("Done. Set this as BURNLINE_DATA_URL (or dataUrl in frontend/config.js):")
    print(f"https://huggingface.co/datasets/{repo_id}/resolve/main")


if __name__ == "__main__":
    main()
