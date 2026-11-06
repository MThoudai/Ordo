"""本地知识切块与词面检索；不调用在线嵌入或生成服务。"""
import math
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def embedding_tokenizer():
    """使用与 Chroma 默认嵌入相同的词表，计数时关闭截断和填充。"""
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    from tokenizers import Tokenizer

    model = ONNXMiniLM_L6_V2()
    model._download_model_if_not_exists()
    tokenizer = Tokenizer.from_file(str(
        Path(model.DOWNLOAD_PATH) / model.EXTRACTED_FOLDER_NAME / "tokenizer.json"
    ))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def chunk_markdown(text, chunk_size=700, max_tokens=240):
    """按章节和段落切块，每块携带父标题，并为 256-token 模型留出余量。"""
    if chunk_size < 1 or max_tokens < 16:
        raise ValueError("chunk_size 必须为正数，max_tokens 至少为16")
    tokenizer = embedding_tokenizer()

    def tokens(value):
        return len(tokenizer.encode(value).ids)

    def fits(value):
        return len(value) <= chunk_size and tokens(value) <= max_tokens

    sections, headings, body = [], [], []
    in_code = None
    for line in text.strip().splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)[0]
            in_code = None if in_code == marker else (in_code or marker)
        heading = re.match(r"^(#{1,6})\s+(.+)$", line) if not in_code and not fence else None
        if heading:
            if any(s.strip() for s in body):
                sections.append((list(headings), "\n".join(body).strip()))
            body = []
            level = len(heading.group(1))
            headings = [h for h in headings if h[0] < level] + [(level, line)]
        else:
            body.append(line)
    if any(s.strip() for s in body):
        sections.append((headings, "\n".join(body).strip()))

    chunks = []
    for parents, content in sections:
        prefix = "\n".join(h[1] for h in parents)
        # 异常长标题不能占满正文预算；标题原文仍保留于源文档。
        while prefix and (tokens(prefix) > max_tokens // 3 or len(prefix) > chunk_size // 3):
            prefix = prefix[:max(0, len(prefix) - max(1, len(prefix) // 8))].rstrip()

        def wrap(value):
            return (prefix + "\n\n" + value).strip() if prefix else value.strip()

        blocks, lines, fence_marker = [], [], None
        for line in content.splitlines():
            fence = re.match(r"^\s*(`{3,}|~{3,})", line)
            if fence:
                marker = fence.group(1)[0]
                fence_marker = None if fence_marker == marker else (fence_marker or marker)
            if not line.strip() and not fence_marker:
                if lines:
                    blocks.append("\n".join(lines).strip())
                    lines = []
            else:
                lines.append(line)
        if lines:
            blocks.append("\n".join(lines).strip())

        current = ""
        for block in blocks:
            candidate = current + "\n\n" + block if current else block
            if fits(wrap(candidate)):
                current = candidate
                continue
            if current:
                chunks.append(wrap(current))
                current = ""
            remaining = block
            while remaining and not fits(wrap(remaining)):
                # 先确定 token/字符安全边界，再优先在换行或句末断开。
                low, high = 1, min(len(remaining), chunk_size)
                while low < high:
                    middle = (low + high + 1) // 2
                    if fits(wrap(remaining[:middle])):
                        low = middle
                    else:
                        high = middle - 1
                cut = low
                if not fits(wrap(remaining[:cut])):
                    raise ValueError("切块预算不足以容纳正文")
                boundaries = [m.end() for m in re.finditer(r"\n|[。！？；]", remaining[:cut])]
                if boundaries and boundaries[-1] >= cut // 2:
                    cut = boundaries[-1]
                piece = remaining[:cut].strip()
                if piece:
                    chunks.append(wrap(piece))
                remaining = remaining[cut:].lstrip()
            current = remaining
        if current:
            chunks.append(wrap(current))
    return chunks


def lexical_tokens(text):
    text = text.lower()
    result = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", text)
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(segment[i:i + 2] for i in range(len(segment) - 1))
    return result


class BM25Index:
    """对当前知识集合构建可重用的中文单字/双字及 ASCII 词面索引。"""

    def __init__(self, records):
        self.records = records
        self.terms = [Counter(lexical_tokens(row["content"])) for row in records]
        self.lengths = [sum(row.values()) for row in self.terms]
        self.average = sum(self.lengths) / max(len(records), 1) or 1
        self.df = Counter(term for row in self.terms for term in row)

    def search(self, query, top_k):
        terms = set(lexical_tokens(query))
        scored = []
        for i, row in enumerate(self.terms):
            score = 0.0
            for term in terms:
                tf = row[term]
                if tf:
                    idf = math.log(1 + (len(self.records) - self.df[term] + .5) / (self.df[term] + .5))
                    score += idf * tf * 2.5 / (tf + 1.5 * (.25 + .75 * self.lengths[i] / self.average))
            if score > 0:
                scored.append((score, self.records[i]))
        scored.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
        return [dict(row, bm25_score=round(score, 6)) for score, row in scored[:top_k]]


def fuse_rankings(rankings, top_k):
    """RRF 合并不同量纲的排名，按稳定片段 ID 去重。"""
    scores, records = {}, {}
    for ranking in rankings:
        seen = set()
        for rank, row in enumerate(ranking, 1):
            key = row["chunk_id"]
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1 / (60 + rank)
            records.setdefault(key, {}).update(row)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))[:top_k]
    return [dict(records[key], score=round(scores[key], 6)) for key in ordered]
