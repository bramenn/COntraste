"""Fingerprints for deduplication: text embeddings (local multilingual ONNX model, no API)
and perceptual image hashes (64-bit dHash)."""
import hashlib
import os
import re
import unicodedata
from functools import lru_cache

import numpy as np
from PIL import Image

EMBED_REPO = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_FILE = "onnx/model_quint8_avx2.onnx"
SIMILAR_TEXT = float(os.getenv("SIMILAR_TEXT_THRESHOLD", "0.77"))
SIMILAR_IMAGE_BITS = 6


@lru_cache(maxsize=1)
def _model():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(hf_hub_download(EMBED_REPO, "tokenizer.json"))
    tok.enable_truncation(256)
    sess = ort.InferenceSession(hf_hub_download(EMBED_REPO, EMBED_FILE), providers=["CPUExecutionProvider"])
    return tok, sess


_FRAMING = re.compile(r"^\s*[¿¡]?\s*(?:es\s+(?:cierto|verdad)|ser[aá]\s+(?:cierto|verdad)|dicen|me\s+dijeron|"
                      r"le[ií]\s+|vi\s+)\s*(?:que)?\s*|[¿?¡!]", re.I)


def embed(text: str) -> np.ndarray:
    """Normalised vector. Strips the question framing ("¿es cierto que…?") so only the fact is compared."""
    tok, sess = _model()
    enc = tok.encode(_FRAMING.sub("", text).strip())
    ids = np.array([enc.ids], dtype=np.int64)
    mask = np.array([enc.attention_mask], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in {i.name for i in sess.get_inputs()}:
        feeds["token_type_ids"] = np.zeros_like(ids)
    out = sess.run(None, feeds)[0][0]
    v = (out * mask[0][:, None]).sum(0) / mask.sum()
    return (v / np.linalg.norm(v)).astype(np.float32)


def cosine(a: bytes, b: np.ndarray) -> float:
    return float(np.frombuffer(a, dtype=np.float32) @ b)


def text_key(text: str) -> str:
    """Hash of the normalised text (no accents, case or punctuation)."""
    t = unicodedata.normalize("NFKD", text.casefold())
    t = re.sub(r"[^a-z0-9ñ ]+", " ", "".join(c for c in t if not unicodedata.combining(c)))
    return hashlib.sha256(" ".join(t.split()).encode()).hexdigest()


def dhash(img: Image.Image) -> int:
    g = np.asarray(img.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()
