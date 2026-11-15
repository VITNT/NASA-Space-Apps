"""Start Burnline on a Hugging Face Space.

The Space has no permanent disk, so the Parquet products are downloaded from the
dataset named in FIRMS_DATASET_REPO each time the Space starts, then served.
"""
from __future__ import annotations

import os

import uvicorn
from huggingface_hub import snapshot_download


def main() -> None:
    dataset = os.environ["FIRMS_DATASET_REPO"]
    print(f"Downloading FIRMS data from {dataset}...", flush=True)
    root = snapshot_download(
        repo_id=dataset,
        repo_type="dataset",
        local_dir=os.environ.get("FIRMS_DOWNLOAD_DIR", "/tmp/firms"),
        allow_patterns=["data/merged/**", "data/catalog/**"],
    )
    os.environ["FIRMS_PROJECT_ROOT"] = root
    uvicorn.run("firms_pipeline.api:app", host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))


if __name__ == "__main__":
    main()
