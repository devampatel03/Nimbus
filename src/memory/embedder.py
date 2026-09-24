# Copyright (c) Nimbus. All rights reserved.
# Private, unpublished, proprietary. Do not distribute.

"""Real sentence-embedding model for episodic memory (Step 11, resolves AD-7).

`Qwen3Embedder` implements the `Embedder` protocol declared in
`src/memory/manager.py` (`embed(text: str) -> list[float]`). It wraps
`sentence-transformers`' `SentenceTransformer` around `Qwen/Qwen3-Embedding-0.6B`
(Apache 2.0, Alibaba/Qwen -- see NOTICE), runs on CPU (no GPU required), and
loads the model lazily on first `embed()` call rather than at import or
`__init__` time, so importing this module (e.g. transitively, via
`src.memory.manager`) never triggers a model download or weight load.

`Qwen3-Embedding-0.6B` was trained with Matryoshka Representation Learning,
so its native 1024-dim output can be truncated to a smaller
`embed_dim` via `SentenceTransformer`'s `truncate_dim` without a separate
projection step; 1024 (the native size) is the default here.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_EMBED_DIM = 1024


class Qwen3Embedder:
    """`Embedder` protocol implementation backed by a real CPU embedding model."""

    def __init__(self, *, model_name: str = MODEL_NAME, embed_dim: int = DEFAULT_EMBED_DIM) -> None:
        self._model_name = model_name
        self._embed_dim = embed_dim
        self._model: SentenceTransformer | None = None

    def _load(self) -> "SentenceTransformer":
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self._model_name,
                device="cpu",
                truncate_dim=self._embed_dim,
            )
        return self._model

    def embed(self, text: str) -> list[float]:
        model = self._load()
        vector = model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
        return vector.tolist()  # type: ignore[no-any-return]
