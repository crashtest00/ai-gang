"""The local embedding model (Corpus Indexing REQ-03, REQ-06; Knowledge
Retrieval REQ-08; build brief §1b and I-08).

Each unit's text is embedded when it is written (``index.store``) and each
question when it is asked (``retrieval.ranking``), by a small sentence
embedding model run in this process with ONNX Runtime. Nothing is sent
anywhere: the model's files are read from ``settings.MEMORY_EMBED_MODEL_DIR``,
which Memory's image fills at build time (``scripts/fetch-embedding-model.sh``,
the same script the test setup runs). This module never downloads. A missing
or incomplete model directory is ``ModelUnavailable``, an error, so a unit
that cannot be embedded is recorded as an indexing failure (REQ-09) and a
question that cannot be embedded fails, rather than either fetching weights.

**Units longer than the model's input.** The model reads at most
``MODEL.max_tokens`` tokens (512, with its two special tokens). A unit is at
most 2,000 characters (a session turn 4,000), which is often more, so a unit
is embedded as overlapping windows of its tokens, each within the limit, and
it scores as its best window (``retrieval.ranking``). No text is cut off.

**The stored form.** A unit's vector is its windows' L2-normalised float32
vectors, one after another, packed as bytes in ``IndexUnit.embedding``
(``pack``/``unpack``). Cosine similarity is then a dot product, computed in
process with numpy: no PostgreSQL extension.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Sequence

import numpy as np
from django.conf import settings


@dataclass(frozen=True)
class ModelSpec:
    name: str
    revision: str
    dim: int
    max_tokens: int
    pooling: str  # 'cls' or 'mean'
    query_prefix: str


# Chosen on the frozen paraphrase set (strategy/v6.0/v6.1/eval-paraphrase/),
# run through this module and retrieval.ranking (tests/test_retrieval_paraphrase.py).
MODEL = ModelSpec(
    name='BAAI/bge-small-en-v1.5',
    revision='5c38ec7c405ec4b44b94cc5a9bb96e735b38267a',
    dim=384,
    max_tokens=512,
    pooling='cls',
    query_prefix='Represent this sentence for searching relevant passages: ',
)

# Tokens two neighbouring windows of one unit share, so a sentence cut by a
# window boundary is whole in one of them.
WINDOW_OVERLAP = 64
BATCH = 16
MODEL_FILES = ('model.onnx', 'tokenizer.json')


class ModelUnavailable(RuntimeError):
    """The model's files are not where the image puts them. Memory never
    fetches them (I-08)."""


class _Model:
    def __init__(self, directory: str):
        missing = [f for f in MODEL_FILES if not os.path.isfile(os.path.join(directory, f))]
        if missing:
            raise ModelUnavailable(
                f"the embedding model {MODEL.name} is not in {directory!r} (missing {', '.join(missing)}). "
                'Memory never downloads it: the image bakes it in at build time, and a host test run fetches '
                'it once with scripts/fetch-embedding-model.sh')
        import onnxruntime
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(os.path.join(directory, 'tokenizer.json'))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.cls = self.tokenizer.token_to_id('[CLS]')
        self.sep = self.tokenizer.token_to_id('[SEP]')
        options = onnxruntime.SessionOptions()
        threads = int(getattr(settings, 'MEMORY_EMBED_THREADS', 0) or 0)
        if threads:
            options.intra_op_num_threads = threads
        self.session = onnxruntime.InferenceSession(os.path.join(directory, 'model.onnx'), options,
                                                    providers=['CPUExecutionProvider'])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def windows(self, text: str) -> list[list[int]]:
        ids = self.tokenizer.encode(text, add_special_tokens=False).ids
        size = MODEL.max_tokens - 2
        step = size - WINDOW_OVERLAP
        starts = range(0, max(len(ids) - WINDOW_OVERLAP, 1), step) if len(ids) > size else [0]
        return [[self.cls] + ids[s:s + size] + [self.sep] for s in starts]

    def encode(self, sequences: list[list[int]]) -> np.ndarray:
        out = np.zeros((len(sequences), MODEL.dim), dtype=np.float32)
        order = sorted(range(len(sequences)), key=lambda i: len(sequences[i]))
        for b in range(0, len(order), BATCH):
            batch = order[b:b + BATCH]
            width = max(len(sequences[i]) for i in batch)
            ids = np.zeros((len(batch), width), dtype=np.int64)
            mask = np.zeros((len(batch), width), dtype=np.int64)
            for row, i in enumerate(batch):
                ids[row, :len(sequences[i])] = sequences[i]
                mask[row, :len(sequences[i])] = 1
            feed = {'input_ids': ids, 'attention_mask': mask}
            if 'token_type_ids' in self.inputs:
                feed['token_type_ids'] = np.zeros_like(ids)
            hidden = self.session.run(None, feed)[0]
            if MODEL.pooling == 'cls':
                pooled = hidden[:, 0]
            else:
                weights = mask[:, :, None].astype(np.float32)
                pooled = (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            out[batch] = pooled / np.maximum(norms, 1e-12)
        return out


_lock = threading.Lock()
_loaded: dict = {}


def model() -> _Model:
    """The model, loaded once per process from the configured directory."""
    directory = settings.MEMORY_EMBED_MODEL_DIR
    with _lock:
        if directory not in _loaded:
            _loaded.clear()
            _loaded[directory] = _Model(directory)
        return _loaded[directory]


def embed_units(texts: Sequence[str]) -> list[bytes]:
    """Each text's packed window vectors (``pack``), in order."""
    m = model()
    windows = [m.windows(t) for t in texts]
    flat = [w for ws in windows for w in ws]
    vectors = m.encode(flat) if flat else np.zeros((0, MODEL.dim), dtype=np.float32)
    out, at = [], 0
    for ws in windows:
        out.append(pack(vectors[at:at + len(ws)]))
        at += len(ws)
    return out


def embed_query(question: str) -> np.ndarray:
    """The question's normalised vector. A question longer than the model's
    input is read to the limit."""
    m = model()
    ids = m.tokenizer.encode(MODEL.query_prefix + question, add_special_tokens=False).ids[:MODEL.max_tokens - 2]
    return m.encode([[m.cls] + ids + [m.sep]])[0]


def pack(vectors: np.ndarray) -> bytes:
    return np.ascontiguousarray(vectors, dtype='<f4').tobytes()


def unpack(blob) -> np.ndarray:
    return np.frombuffer(bytes(blob), dtype='<f4').reshape(-1, MODEL.dim)
