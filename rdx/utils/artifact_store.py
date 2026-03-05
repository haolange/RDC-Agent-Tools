"""
RDX-MCP 的内容寻址 artifact 存储（CAS）。

使用 SHA256 hash 作为 key 存储文件（images、shader dumps、readback data 等），
磁盘布局类似 git object storage：

    <store_root>/<sha256[:2]>/<sha256[2:4]>/<sha256>

Artifacts 通过 ``rdx://`` URI 引用，并由 :class:`rdx.models.ArtifactRef`
描述。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
import aiofiles.os

from rdx.models import ArtifactRef

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers（辅助）
# ---------------------------------------------------------------------------

_CHUNK_SIZE = 1 << 16  # 64 KiB read chunks


def _shard_path(root: Path, sha256: str) -> Path:
    """返回给定 hash 的两级分片路径。

    Example::

        _shard_path(Path("/store"), "abcdef01...") -> Path("/store/ab/cd/abcdef01...")
    """
    return root / sha256[:2] / sha256[2:4] / sha256


def _build_uri(sha256: str) -> str:
    """构建 artifact 的标准 ``rdx://`` URI。"""
    return f"rdx://artifacts/{sha256[:2]}/{sha256[2:4]}/{sha256}"


def _sha256_bytes(data: bytes) -> str:
    """计算 *data* 的 SHA256（hex 编码）。"""
    return hashlib.sha256(data).hexdigest()


async def _sha256_file(path: Path) -> str:
    """计算 *path* 文件的 SHA256（hex 编码），避免一次性读入内存。"""
    h = hashlib.sha256()
    async with aiofiles.open(path, "rb") as fh:
        while True:
            chunk = await fh.read(_CHUNK_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# ArtifactStore
# ---------------------------------------------------------------------------


class ArtifactStore:
    """基于目录的内容寻址存储（CAS）。

    每个 blob 仅存一次，位于以 SHA256 摘要分片的两级目录下。
    元数据由返回的 :class:`ArtifactRef` 在内存中携带；存储本身保持简洁、
    无状态，便于在多个 async 任务中安全使用。

    Parameters
    ----------
    root:
        存储根目录。首次写入时若不存在会自动创建。
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    # -- properties ---------------------------------------------------------

    @property
    def root(self) -> Path:
        """存储根目录。"""
        return self._root

    # -- public API ---------------------------------------------------------

    async def store(
        self,
        data: bytes,
        mime: str = "application/octet-stream",
        suffix: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        """存储原始字节并返回 :class:`ArtifactRef`。

        若磁盘上已存在相同 SHA256 的 artifact，将跳过写入
        （内容寻址去重）。

        Parameters
        ----------
        data:
            待存储的原始字节。
        mime:
            artifact 的 MIME 类型（如 ``image/png``）。
        meta:
            附加到返回引用的任意元数据字典。

        Returns
        -------
        ArtifactRef
            A reference containing the ``rdx://`` URI, SHA256, MIME type,
            byte length, and metadata.
        """
        sha = _sha256_bytes(data)
        dest = _shard_path(self._root, sha)

        if not dest.exists():
            await aiofiles.os.makedirs(dest.parent, exist_ok=True)
            # Write to a temporary file first, then rename for atomicity.
            tmp = dest.with_suffix(".tmp")
            try:
                async with aiofiles.open(tmp, "wb") as fh:
                    await fh.write(data)
                await aiofiles.os.rename(tmp, dest)
            except BaseException:
                # Clean up partial write on any failure.
                try:
                    await aiofiles.os.remove(tmp)
                except OSError:
                    pass
                raise
            logger.debug("Stored artifact %s (%d bytes)", sha[:12], len(data))
        else:
            logger.debug("Artifact %s already present, skipping write", sha[:12])

        return ArtifactRef(
            uri=_build_uri(sha),
            sha256=sha,
            mime=mime,
            bytes=len(data),
            meta={
                **(meta or {}),
                **({"suffix": suffix} if suffix else {}),
                "stored_ts": int(time.time() * 1000),
            },
        )

    async def store_file(
        self,
        path: Path,
        mime: str = "application/octet-stream",
        suffix: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        """对磁盘文件计算 hash 并存入 CAS。

        对大文件采用流式 hash 计算，避免一次性加载到内存。
        随后将实际文件内容拷贝到分片目录。

        Parameters
        ----------
        path:
            源文件路径。
        mime:
            artifact 的 MIME 类型。
        meta:
            附加到返回引用的任意元数据字典。

        Returns
        -------
        ArtifactRef

        Raises
        ------
        FileNotFoundError
            当 *path* 不存在时抛出。
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"Source file does not exist: {path}")

        sha = await _sha256_file(path)
        dest = _shard_path(self._root, sha)
        file_size = path.stat().st_size

        if not dest.exists():
            await aiofiles.os.makedirs(dest.parent, exist_ok=True)
            tmp = dest.with_suffix(".tmp")
            try:
                async with aiofiles.open(path, "rb") as src_fh, \
                           aiofiles.open(tmp, "wb") as dst_fh:
                    while True:
                        chunk = await src_fh.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        await dst_fh.write(chunk)
                await aiofiles.os.rename(tmp, dest)
            except BaseException:
                try:
                    await aiofiles.os.remove(tmp)
                except OSError:
                    pass
                raise
            logger.debug(
                "Stored artifact %s from %s (%d bytes)", sha[:12], path, file_size
            )
        else:
            logger.debug("Artifact %s already present, skipping copy", sha[:12])

        return ArtifactRef(
            uri=_build_uri(sha),
            sha256=sha,
            mime=mime,
            bytes=file_size,
            meta={
                **(meta or {}),
                **({"suffix": suffix} if suffix else {}),
                "stored_ts": int(time.time() * 1000),
            },
        )

    async def store_json(
        self,
        data: Dict[str, Any],
        *,
        name: str = "",
        session_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        merged = dict(meta or {})
        if name:
            merged["name"] = name
        if session_id:
            merged["session_id"] = session_id
        return await self.store(
            payload,
            mime="application/json",
            suffix=".json",
            meta=merged,
        )

    async def store_text(
        self,
        text: str,
        *,
        name: str = "",
        session_id: Optional[str] = None,
        mime: str = "text/plain",
        suffix: str = ".txt",
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        merged = dict(meta or {})
        if name:
            merged["name"] = name
        if session_id:
            merged["session_id"] = session_id
        return await self.store(
            text.encode("utf-8"),
            mime=mime,
            suffix=suffix,
            meta=merged,
        )

    async def store_image(
        self,
        image: Any,
        *,
        name: str = "",
        session_id: Optional[str] = None,
        fmt: str = "PNG",
        meta: Optional[Dict[str, Any]] = None,
    ) -> ArtifactRef:
        from PIL import Image

        if isinstance(image, bytes):
            payload = image
            suffix = ".png"
            mime = "image/png"
        else:
            img = image if isinstance(image, Image.Image) else Image.fromarray(image)
            buf = io.BytesIO()
            fmt_upper = fmt.upper()
            img.save(buf, format=fmt_upper)
            payload = buf.getvalue()
            if fmt_upper == "JPEG":
                suffix = ".jpg"
                mime = "image/jpeg"
            elif fmt_upper == "EXR":
                suffix = ".exr"
                mime = "image/x-exr"
            else:
                suffix = ".png"
                mime = "image/png"

        merged = dict(meta or {})
        if name:
            merged["name"] = name
        if session_id:
            merged["session_id"] = session_id
        return await self.store(payload, mime=mime, suffix=suffix, meta=merged)

    def list_artifacts(self, prefix: str = "") -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if not self._root.exists():
            return items
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix == ".tmp":
                continue
            rel = str(path.relative_to(self._root)).replace("\\", "/")
            if prefix and not rel.startswith(prefix):
                continue
            st = path.stat()
            items.append(
                {
                    "path": str(path),
                    "relative_path": rel,
                    "byte_size": int(st.st_size),
                    "created_ts": int(st.st_ctime * 1000),
                    "modified_ts": int(st.st_mtime * 1000),
                },
            )
        items.sort(key=lambda it: it["modified_ts"], reverse=True)
        return items

    def cleanup_artifacts(
        self,
        *,
        older_than_ms: Optional[int] = None,
        prefix: str = "",
        max_total_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        now_ms = int(time.time() * 1000)
        artifacts = self.list_artifacts(prefix=prefix)
        deleted: List[str] = []
        freed = 0

        if older_than_ms is not None:
            for entry in artifacts:
                age = now_ms - int(entry["modified_ts"])
                if age < older_than_ms:
                    continue
                p = Path(entry["path"])
                try:
                    size = int(entry["byte_size"])
                    p.unlink(missing_ok=True)
                    deleted.append(str(p))
                    freed += size
                except OSError:
                    continue
            artifacts = self.list_artifacts(prefix=prefix)

        if max_total_bytes is not None:
            total = sum(int(it["byte_size"]) for it in artifacts)
            if total > max_total_bytes:
                by_oldest = sorted(artifacts, key=lambda it: it["modified_ts"])
                for entry in by_oldest:
                    if total <= max_total_bytes:
                        break
                    p = Path(entry["path"])
                    try:
                        size = int(entry["byte_size"])
                        p.unlink(missing_ok=True)
                        deleted.append(str(p))
                        freed += size
                        total -= size
                    except OSError:
                        continue

        return {"deleted": deleted, "freed_bytes": freed}

    async def retrieve(self, sha256: str) -> bytes:
        """按 SHA256 digest 读取 artifact 的原始字节。

        Parameters
        ----------
        sha256:
            Hex 编码的 SHA256 digest。

        Returns
        -------
        bytes

        Raises
        ------
        FileNotFoundError
            当 store 中不存在该 hash 的 artifact 时抛出。
        """
        dest = _shard_path(self._root, sha256)
        if not dest.is_file():
            raise FileNotFoundError(
                f"Artifact not found in store: {sha256}"
            )
        async with aiofiles.open(dest, "rb") as fh:
            return await fh.read()

    def get_path(self, sha256: str) -> Path:
        """返回 artifact 在文件系统中的存放路径。

        该方法 **不** 保证文件存在；如需检查请使用 :meth:`exists`。

        Parameters
        ----------
        sha256:
            Hex 编码的 SHA256 digest。

        Returns
        -------
        Path
        """
        return _shard_path(self._root, sha256)

    def exists(self, sha256: str) -> bool:
        """检查给定 hash 的 artifact 是否存在。 

        Parameters
        ----------
        sha256:
            Hex 编码的 SHA256 digest。

        Returns
        -------
        bool
        """
        return _shard_path(self._root, sha256).is_file()
