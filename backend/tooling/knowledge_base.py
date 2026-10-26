"""SaaS 产品资料的 Markdown 导入与 ChromaDB 向量检索。

知识检索只访问 saas_knowledge_base 产品知识集合。
默认向量由客户端的 all-MiniLM-L6-v2 ONNX 模型生成，并交给 ChromaDB 存储。
产品知识与 episodic/user_profile 对话记忆使用独立集合。
"""
import asyncio
import hashlib
import json
import logging
import re
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    _WRITE_LOCK = threading.RLock()
    COLLECTION_NAME = "saas_knowledge_base"
    CORPUS_ID = "ordo_saas"
    CORPUS_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "saas"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        load_local_documents: bool = True,
        allow_local_fallback: bool = True,
    ):
        self._use_server = False
        try:
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info("知识库 ChromaDB 已连接: %s:%s", chroma_host, chroma_port)
        except Exception:
            if not allow_local_fallback or os.getenv("ORDO_ENV", "development").lower() == "production":
                raise
            logger.warning("知识库服务不可用，使用本地 SaaS 集合: %s", chroma_path)
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # HttpClient 同样在客户端生成向量；镜像已预缓存该模型。
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={
                "description": "Ordo SaaS 产品知识库",
                "hnsw:space": "cosine",
                "embedding_model": "all-MiniLM-L6-v2",
            },
        )
        self.imported_chunks = 0
        if load_local_documents:
            # 只加载知识清单中登记的 SaaS Markdown。
            self.imported_chunks = self.import_saas_documents()

    @classmethod
    def load_saas_documents(cls) -> List[Dict[str, Any]]:
        """完整读入并校验清单，读取失败前不进行任何索引删除。"""
        manifest = json.loads((cls.CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("corpus_id") != cls.CORPUS_ID or not manifest.get("version"):
            raise ValueError("SaaS 知识清单缺少正确的 corpus_id/version")
        entries = manifest.get("documents", [])
        if not entries:
            raise ValueError("SaaS 知识清单不能为空")
        documents = []
        seen_ids = set()
        for entry in entries:
            filename = entry["file"]
            if Path(filename).name != filename or not filename.endswith(".md"):
                raise ValueError("知识文件必须是清单目录内的 Markdown 文件")
            path = (cls.CORPUS_DIR / filename).resolve()
            if path.parent != cls.CORPUS_DIR.resolve():
                raise ValueError("知识文件不能指向清单目录之外")
            document_id = entry["document_id"]
            if not document_id or document_id in seen_ids:
                raise ValueError("清单 document_id 必须非空且唯一")
            seen_ids.add(document_id)
            content = path.read_text(encoding="utf-8").strip()
            if not entry.get("title") or not content:
                raise ValueError("知识文档标题或正文为空")
            documents.append({
                "document_id": document_id,
                "title": entry["title"],
                "content": content,
                "source": f"knowledge/saas/{filename}",
                "version": manifest["version"],
                # 清单中的角色标签与当前Agent标识一致。
                "agents": ",".join(dict.fromkeys(
                    role.strip()
                    for role in entry.get("agents", "").split(",") if role.strip()
                )),
                "corpus_id": cls.CORPUS_ID,
            })
        return documents

    def import_saas_documents(self) -> int:
        documents = self.load_saas_documents()
        count = self.add_documents(documents)
        # 只有本次清单全部写入后，才清理从该清单移除的资料。
        current = {doc["document_id"] for doc in documents}
        existing = self._collection.get(where={"corpus_id": self.CORPUS_ID}, include=["metadatas"])
        stale_ids = [
            chunk_id for chunk_id, metadata in zip(existing["ids"], existing["metadatas"])
            if metadata.get("document_id") not in current
        ]
        if stale_ids:
            self._collection.delete(ids=stale_ids)
        logger.info("SaaS 本地资料已导入: %s 篇，%s 片段", len(documents), count)
        return count

    def add_documents(self, documents: List[Dict[str, Any]]) -> int:
        """按 Markdown 结构切块、生成向量并幂等写入当前 SaaS 集合。"""
        with self._WRITE_LOCK:
            return self._add_documents_locked(documents)

    def _add_documents_locked(self, documents):
        for doc in documents:
            if not isinstance(doc, dict) or not isinstance(doc.get("title"), str) or not isinstance(doc.get("content"), str):
                raise ValueError("文档需要字符串title和content")
            if not doc["title"].strip() or not doc["content"].strip():
                raise ValueError("文档标题和正文不能为空")
            if doc.get("document_id") is not None and (not isinstance(doc["document_id"], str) or not doc["document_id"].strip()):
                raise ValueError("document_id必须为非空字符串")
            if doc.get("corpus_id") != self.CORPUS_ID and str(doc.get("document_id", "")).startswith("saas-"):
                raise ValueError("本地SaaS清单ID不可由手工上传覆盖")
        total = 0
        for doc in documents:
            title = doc.get("title", "").strip()
            content = doc.get("content", "").strip()
            if not title or not content:
                raise ValueError("文档标题和正文不能为空")
            explicit = doc.get("document_id")
            document_id = explicit if doc.get("corpus_id") == self.CORPUS_ID else (
                "manual-id-" + hashlib.sha256(explicit.encode()).hexdigest() if explicit else
                "upload-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:24])
            version = str(doc.get("version", "local"))
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            chunks = self._chunk_text(content)
            ids = [hashlib.sha256(f"{document_id}:{version}:{checksum}:{i}".encode("utf-8")).hexdigest() for i in range(len(chunks))]
            metadata = [{
                "title": title,
                "document_id": document_id,
                "version": version,
                "checksum": checksum,
                "chunk_id": ids[i],
                "chunk_index": i,
                "total_chunks": len(chunks),
                "source": doc.get("source", "manual_upload"),
                "corpus_id": doc.get("corpus_id", "manual_upload"),
                "agents": doc.get("agents", "general,collaboration,technical"),
                "knowledge_domain": "saas",
            } for i in range(len(chunks))]
            # 写入成功以后删除同文档旧片段，失败不提前移除有效资料。
            self._collection.upsert(ids=ids, documents=chunks, metadatas=metadata)
            previous = self._collection.get(where={"document_id": document_id}, include=[])
            obsolete = sorted(set(previous["ids"]) - set(ids))
            if obsolete:
                self._collection.delete(ids=obsolete)
            total += len(ids)
        return total

    async def add_documents_async(self, documents: List[Dict[str, Any]]) -> int:
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """仅检索 SaaS 集合；score 为 1-cosine_distance，不是正确概率。"""
        count = self._collection.count()
        if not query.strip() or count == 0:
            return []
        top_k = max(1, min(int(top_k), 20, count))
        results = self._collection.query(query_texts=[query], n_results=top_k)
        items = []
        for chunk_id, content, meta, distance in zip(
            results["ids"][0], results["documents"][0], results["metadatas"][0], results["distances"][0],
        ):
            meta = meta or {}
            items.append({
                "title": meta.get("title", ""),
                "content": content,
                "score": round(1.0 - distance, 4),
                "chunk": meta.get("chunk_index", 0),
                "chunk_id": chunk_id,
                "document_id": meta.get("document_id", ""),
                "version": meta.get("version", ""),
                "source": meta.get("source", ""),
                "collection": self.COLLECTION_NAME,
            })
        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        return await asyncio.to_thread(self._collection.count)

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        return await self.search_async(params.get("query", ""), top_k=params.get("top_k", 5))

    @staticmethod
    def _chunk_text(text: str, chunk_size: int = 700) -> List[str]:
        """保留 Markdown 标题、段落、表格及代码块；有界分割超长块。"""
        if chunk_size < 1:
            raise ValueError("chunk_size 必须为正数")
        blocks, lines, in_code = [], [], False
        for line in text.strip().splitlines():
            if line.lstrip().startswith("```"):
                in_code = not in_code
            if not in_code and not line.strip():
                if lines:
                    blocks.append("\n".join(lines))
                    lines = []
            else:
                lines.append(line)
        if lines:
            blocks.append("\n".join(lines))

        chunks, current = [], ""
        for block in blocks:
            # 不把不同主题凑成一个长向量：按标题边界保留完整规则章节。
            # 文档主标题与首个章节合并，避免单独索引一个没有正文的标题。
            if re.match(r"^#{1,3} ", block) and current and not re.fullmatch(r"#[^#\n]+", current):
                chunks.append(current)
                current = ""
            # 一般规则、表格、示例都在上限内；超长块按换行/句子再分。
            pieces = [block] if len(block) <= chunk_size else re.split(r"(?<=。)|(?<=\n)", block)
            for piece in pieces:
                piece = piece.strip()
                if not piece:
                    continue
                while len(piece) > chunk_size:
                    if current:
                        chunks.append(current)
                        current = ""
                    chunks.append(piece[:chunk_size])
                    piece = piece[chunk_size:]
                if not piece:
                    continue
                candidate = f"{current}\n\n{piece}" if current else piece
                if len(candidate) > chunk_size:
                    chunks.append(current)
                    current = piece
                else:
                    current = candidate
        if current:
            chunks.append(current)
        return chunks
