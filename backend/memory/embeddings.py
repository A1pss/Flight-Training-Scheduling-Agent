"""嵌入函数。**摄取期固定跑 CPU**（业务方 2026-08-08 确认的隔离方案）。

GPU 3 上常驻着 Ollama 的 qwen2.5:14b（约 13GB / 24GB），M7 微调窗口还要占同
一张卡。摄取语料极小（四份 PDF ≈ 百级 chunk），CPU 跑一次是秒级到十几秒量级，
没有任何理由去和 Ollama 抢显存 —— 而且 CPU 推理不受显存碎片影响，逐字节可
复现（铁律 9）。设备由 `EMBED_DEVICE` 控制，M5 检索窗口若需要 GPU 可单独切。

两个实现：

- :class:`BGEM3Embedder` —— 真模型（`.data/models/bge-m3`），离线加载
- :class:`HashEmbedder` —— 确定性哈希嵌入，**给 CI 用**

CI 里没有那 2.2GB 权重（`.data/` 已 gitignore），所以默认 provider 由
`EMBED_PROVIDER` 决定：本机 `bge`，CI `hash`。这不是「用假的糊过去」——
`HashEmbedder` 只在「需要一个确定性向量空间来验证 Chroma 读写与 metadata
过滤」的场合使用，检索质量指标（§12.4）一律用真模型跑，且那是 M5 的事。
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Any, Protocol, runtime_checkable

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

#: bge-m3 的输出维度
BGE_M3_DIM = 1024
#: HashEmbedder 的维度，刻意与 bge-m3 一致，换实现不用重建 collection
HASH_DIM = 1024


@runtime_checkable
class Embedder(Protocol):
    """嵌入函数契约。"""

    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """把一批文本编码成等长向量。"""
        ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


class HashEmbedder:
    """确定性哈希嵌入。同样输入永远同样向量，不需要任何权重文件。

    做法：把文本切成字符 3-gram，每个 gram 用 blake2b 映射到一个维度并累加权重，
    最后 L2 归一化。它捕捉不到语义，但**是个真正的度量空间** —— 相同文本距离
    为 0、共享子串的文本距离更近，足以验证 Chroma 的写入、查询与 metadata 过滤。
    """

    name = "hash-3gram"
    dim = HASH_DIM

    def __init__(self, dim: int = HASH_DIM) -> None:
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        padded = f"  {text}  "
        for i in range(len(padded) - 2):
            gram = padded[i : i + 3]
            digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
            index = struct.unpack("<Q", digest)[0] % self.dim
            # 符号也由哈希决定，避免所有维度同号导致向量塌缩
            sign = 1.0 if digest[0] & 1 else -1.0
            vector[index] += sign
        return _l2_normalize(vector)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class BGEM3Embedder:
    """bge-m3 的离线封装，**固定 CPU**（除非显式传 device）。"""

    name = "bge-m3"
    dim = BGE_M3_DIM

    def __init__(self, model_path: str | None = None, device: str | None = None) -> None:
        settings = get_settings()
        self._model_path = model_path or str(settings.BGE_M3_PATH)
        self._device = device or settings.EMBED_DEVICE
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("加载 bge-m3", path=self._model_path, device=self._device)
            self._model = SentenceTransformer(
                self._model_path, device=self._device, local_files_only=True
            )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True
        )
        return [[float(v) for v in row] for row in vectors]


def build_embedder() -> Embedder:
    """按 `EMBED_PROVIDER` 造嵌入器。"""
    settings = get_settings()
    if settings.EMBED_PROVIDER == "hash":
        return HashEmbedder()
    return BGEM3Embedder()


__all__ = [
    "BGE_M3_DIM",
    "HASH_DIM",
    "BGEM3Embedder",
    "Embedder",
    "HashEmbedder",
    "build_embedder",
]
