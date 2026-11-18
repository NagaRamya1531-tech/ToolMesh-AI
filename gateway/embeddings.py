"""Explicitly provisioned CPU embeddings for discovery."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download
from model2vec import StaticModel

REPOSITORY = "minishlab/potion-base-8M"
REVISION = "bf8b056651a2c21b8d2565580b8569da283cab23"
MODEL_ID = f"{REPOSITORY}@{REVISION}"
DIMENSIONS = 256
DIGESTS = {
    "config.json": "2a6ac0e9aaa356a68a5688070db78fc3a464fefe85d2f06a1905ce3718687553",
    "model.safetensors": "f65d0f325faadc1e121c319e2faa41170d3fa07d8c89abd48ca5358d9a223de2",
    "tokenizer.json": "e67e803f624fb4d67dea1c730d06e1067e1b14d830e2c2202569e3ef0f70bb50",
}


def verify(path: Path) -> None:
    for name, digest in DIGESTS.items():
        with (path / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Embedding artifact checksum mismatch")


def download(output: Path) -> None:
    snapshot_download(
        REPOSITORY,
        revision=REVISION,
        local_dir=output,
        allow_patterns=list(DIGESTS),
        token=False,
        max_workers=2,
    )
    verify(output)
    model = StaticModel.from_pretrained(output, normalize=True)
    if model.encode(["model validation"]).shape != (1, DIMENSIONS):
        raise ValueError("Unexpected embedding dimensions")
    (output / "toolmesh-model.json").write_text(
        json.dumps({"model": MODEL_ID, "dimensions": DIMENSIONS})
    )


class Embeddings:
    def __init__(self, path: Path):
        self.path = path
        self.model: StaticModel | None = None
        self.status = "not_installed"

    async def load(self):
        manifest = self.path / "toolmesh-model.json"
        if not manifest.is_file():
            return
        try:
            metadata = json.loads(manifest.read_text())
            if (
                metadata.get("model") != MODEL_ID
                or metadata.get("dimensions") != DIMENSIONS
            ):
                raise ValueError("Unexpected embedding model")
            await asyncio.to_thread(verify, self.path)
            self.model = await asyncio.to_thread(
                StaticModel.from_pretrained, self.path, normalize=True
            )
            vectors = await self.encode(["model validation"])
            if len(json.loads(vectors[0])) != DIMENSIONS:
                raise ValueError("Unexpected embedding dimensions")
        except (OSError, ValueError, RuntimeError):
            self.model = None
            self.status = "invalid"
        else:
            self.status = "ready"

    async def encode(self, documents: list[str]) -> list[str]:
        if self.model is None:
            raise RuntimeError("EMBEDDING_MODEL_UNAVAILABLE")
        vectors = await asyncio.to_thread(self.model.encode, documents, max_length=128)
        return [
            json.dumps(vector.tolist(), separators=(",", ":"), allow_nan=False)
            for vector in vectors
        ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".models/discovery"))
    download(parser.parse_args().output)
