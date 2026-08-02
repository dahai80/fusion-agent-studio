"""Data ingestion — document readers, ETL pipeline, chunking strategies.

Reads plain text, markdown, JSON, CSV files; splits into chunks
using fixed-size, sentence-boundary, or semantic overlap strategies;
pipelines through extract -> transform -> load with pluggable stages.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".html", ".pdf"}


@dataclass
class Document:
    id: str = ""
    content: str = ""
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            source=data.get("source", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class Chunk:
    id: str = ""
    content: str = ""
    document_id: str = ""
    index: int = 0
    start_char: int = 0
    end_char: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "document_id": self.document_id,
            "index": self.index,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chunk:
        return cls(
            id=data.get("id", ""),
            content=data.get("content", ""),
            document_id=data.get("document_id", ""),
            index=data.get("index", 0),
            start_char=data.get("start_char", 0),
            end_char=data.get("end_char", 0),
            metadata=data.get("metadata", {}),
        )


class DocumentReader:
    """Read files into Document objects."""

    def read_file(self, path: str | Path) -> Document:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        ext = path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Unsupported file type: {ext}")

        reader_map = {
            ".txt": self._read_text,
            ".md": self._read_text,
            ".json": self._read_json,
            ".csv": self._read_csv,
            ".html": self._read_html,
        }
        content, meta = reader_map[ext](path)
        meta["file_extension"] = ext
        meta["file_name"] = path.name
        logger.info("Read document: %s (%d chars)", path.name, len(content))
        return Document(content=content, source=str(path), metadata=meta)

    def read_text(
        self, text: str, source: str = "inline", metadata: dict | None = None
    ) -> Document:
        return Document(content=text, source=source, metadata=metadata or {})

    def _read_text(self, path: Path) -> tuple[str, dict]:
        content = path.read_text(encoding="utf-8", errors="replace")
        return content, {"size_bytes": path.stat().st_size}

    def _read_json(self, path: Path) -> tuple[str, dict]:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, (dict, list)):
            content = json.dumps(data, indent=2, ensure_ascii=False)
        else:
            content = str(data)
        meta = {"json_type": type(data).__name__}
        if isinstance(data, list):
            meta["item_count"] = len(data)
        elif isinstance(data, dict):
            meta["keys"] = list(data.keys())[:20]
        return content, meta

    def _read_csv(self, path: Path) -> tuple[str, dict]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(raw))
        rows = list(reader)
        content = json.dumps(rows, indent=2, ensure_ascii=False)
        meta = {"row_count": len(rows)}
        if rows:
            meta["columns"] = list(rows[0].keys())
        return content, meta

    def _read_html(self, path: Path) -> tuple[str, dict]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        return text, {"original_size": len(raw)}


class ChunkingStrategy:
    """Base chunking strategy."""

    def chunk(self, document: Document, **kwargs) -> list[Chunk]:
        raise NotImplementedError


class FixedSizeChunker(ChunkingStrategy):
    """Split document into fixed-size chunks with optional overlap."""

    def __init__(self, chunk_size: int = 500, overlap: int = 50):
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(self, document: Document, **kwargs) -> list[Chunk]:
        chunk_size = kwargs.get("chunk_size", self.chunk_size)
        overlap = kwargs.get("overlap", self.overlap)
        text = document.content
        chunks = []
        start = 0
        idx = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunk_text = text[start:end]
            chunks.append(
                Chunk(
                    document_id=document.id,
                    content=chunk_text,
                    index=idx,
                    start_char=start,
                    end_char=end,
                    metadata={"strategy": "fixed", "chunk_size": chunk_size},
                )
            )
            idx += 1
            step = chunk_size - overlap
            if step <= 0:
                break
            start += step
        logger.debug("Fixed chunked doc %s: %d chunks", document.id, len(chunks))
        return chunks


class SentenceChunker(ChunkingStrategy):
    """Split document at sentence boundaries."""

    _SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+")

    def __init__(self, max_chunk_size: int = 800, min_chunk_size: int = 100):
        self.max_chunk_size = max_chunk_size
        self.min_chunk_size = min_chunk_size

    def chunk(self, document: Document, **kwargs) -> list[Chunk]:
        max_size = kwargs.get("max_chunk_size", self.max_chunk_size)
        min_size = kwargs.get("min_chunk_size", self.min_chunk_size)
        sentences = self._SENTENCE_RE.split(document.content)
        chunks = []
        current = ""
        idx = 0
        start_char = 0

        for sent in sentences:
            if len(current) + len(sent) > max_size and len(current) >= min_size:
                end_char = start_char + len(current)
                chunks.append(
                    Chunk(
                        document_id=document.id,
                        content=current.strip(),
                        index=idx,
                        start_char=start_char,
                        end_char=end_char,
                        metadata={"strategy": "sentence"},
                    )
                )
                idx += 1
                start_char = end_char
                current = sent
            else:
                current += " " + sent if current else sent

        if current.strip():
            chunks.append(
                Chunk(
                    document_id=document.id,
                    content=current.strip(),
                    index=idx,
                    start_char=start_char,
                    end_char=start_char + len(current),
                    metadata={"strategy": "sentence"},
                )
            )

        logger.debug("Sentence chunked doc %s: %d chunks", document.id, len(chunks))
        return chunks


class MarkdownChunker(ChunkingStrategy):
    """Split markdown at heading boundaries."""

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    def chunk(self, document: Document, **kwargs) -> list[Chunk]:
        text = document.content
        headings = list(self._HEADING_RE.finditer(text))
        chunks = []
        idx = 0

        if not headings:
            fallback = FixedSizeChunker()
            return fallback.chunk(document, **kwargs)

        prev_end = 0
        for i, match in enumerate(headings):
            if match.start() > prev_end:
                pre_text = text[prev_end : match.start()].strip()
                if pre_text:
                    chunks.append(
                        Chunk(
                            document_id=document.id,
                            content=pre_text,
                            index=idx,
                            start_char=prev_end,
                            end_char=match.start(),
                            metadata={"strategy": "markdown", "section": "preamble"},
                        )
                    )
                    idx += 1

            section_end = (
                headings[i + 1].start() if i + 1 < len(headings) else len(text)
            )
            section_text = text[match.start() : section_end].strip()
            heading_level = len(match.group(1))
            heading_text = match.group(2)
            chunks.append(
                Chunk(
                    document_id=document.id,
                    content=section_text,
                    index=idx,
                    start_char=match.start(),
                    end_char=section_end,
                    metadata={
                        "strategy": "markdown",
                        "heading": heading_text,
                        "heading_level": heading_level,
                    },
                )
            )
            idx += 1
            prev_end = section_end

        logger.debug("Markdown chunked doc %s: %d chunks", document.id, len(chunks))
        return chunks


TransformFn = Callable[[Document], Document]
ChunkTransformFn = Callable[[Chunk], Chunk]


class ETLPipeline:
    """Extract-Transform-Load pipeline for document processing."""

    def __init__(self):
        self._doc_transforms: list[TransformFn] = []
        self._chunk_transforms: list[ChunkTransformFn] = []
        self._chunker: ChunkingStrategy = FixedSizeChunker()

    def add_doc_transform(self, fn: TransformFn) -> ETLPipeline:
        self._doc_transforms.append(fn)
        return self

    def add_chunk_transform(self, fn: ChunkTransformFn) -> ETLPipeline:
        self._chunk_transforms.append(fn)
        return self

    def set_chunker(self, strategy: ChunkingStrategy) -> ETLPipeline:
        self._chunker = strategy
        return self

    def process(self, document: Document, **chunk_kwargs) -> list[Chunk]:
        doc = document
        for fn in self._doc_transforms:
            doc = fn(doc)
            if doc is None:
                logger.warning("Doc transform returned None, skipping")
                return []

        chunks = self._chunker.chunk(doc, **chunk_kwargs)

        for fn in self._chunk_transforms:
            chunks = [fn(c) for c in chunks]
            chunks = [c for c in chunks if c is not None]

        logger.info(
            "ETL pipeline: doc %s -> %d chunks (%d doc transforms, %d chunk transforms)",
            doc.id,
            len(chunks),
            len(self._doc_transforms),
            len(self._chunk_transforms),
        )
        return chunks

    def process_file(self, path: str | Path, **chunk_kwargs) -> list[Chunk]:
        reader = DocumentReader()
        doc = reader.read_file(path)
        return self.process(doc, **chunk_kwargs)

    def process_text(
        self, text: str, source: str = "inline", **chunk_kwargs
    ) -> list[Chunk]:
        reader = DocumentReader()
        doc = reader.read_text(text, source=source)
        return self.process(doc, **chunk_kwargs)


def strip_whitespace(doc: Document) -> Document:
    doc.content = re.sub(r"\s+", " ", doc.content).strip()
    return doc


def truncate(max_chars: int = 10000) -> TransformFn:
    def _truncate(doc: Document) -> Document:
        if len(doc.content) > max_chars:
            doc.content = doc.content[:max_chars]
            doc.metadata["truncated"] = True
        return doc

    return _truncate


def add_metadata(key: str, value: Any) -> TransformFn:
    def _add(doc: Document) -> Document:
        doc.metadata[key] = value
        return doc

    return _add


def filter_empty_chunks(chunk: Chunk) -> Chunk | None:
    if not chunk.content.strip():
        return None
    return chunk


def normalize_chunk_text(chunk: Chunk) -> Chunk:
    chunk.content = re.sub(r"\s+", " ", chunk.content).strip()
    return chunk


class WebReader:
    def read_url(self, url: str, timeout: int = 30) -> Document:
        logger.info("WebReader fetching: %s", url)
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "FusionAgentStudio/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            content = self._strip_html(html)
            return Document(
                content=content,
                source=url,
                metadata={
                    "reader": "web",
                    "url": url,
                    "content_length": len(content),
                    "original_size": len(html),
                },
            )
        except Exception as e:
            logger.error("WebReader failed for %s: %s", url, e)
            return Document(
                content="",
                source=url,
                metadata={"reader": "web", "url": url, "error": str(e)},
            )

    def read_urls(self, urls: list[str], timeout: int = 30) -> list[Document]:
        docs = []
        for url in urls:
            docs.append(self.read_url(url, timeout=timeout))
        logger.info(
            "WebReader batch: %d URLs, %d succeeded",
            len(urls),
            sum(1 for d in docs if not d.metadata.get("error")),
        )
        return docs

    def _strip_html(self, html: str) -> str:
        text = re.sub(
            r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(
            r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE
        )
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</li>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</h[1-6]>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&quot;", '"', text)
        text = re.sub(r"&#39;", "'", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text


@dataclass
class GitHubFileResult:
    path: str = ""
    content: str = ""
    sha: str = ""
    size: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "sha": self.sha,
            "size": self.size,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GitHubFileResult:
        return cls(
            path=data.get("path", ""),
            content=data.get("content", ""),
            sha=data.get("sha", ""),
            size=data.get("size", 0),
            metadata=data.get("metadata", {}),
        )


class GitHubReader:
    def __init__(self, token: str = "", base_url: str = "https://api.github.com"):
        self.token = token
        self.base_url = base_url.rstrip("/")

    def read_repo_file(
        self, owner: str, repo: str, path: str, ref: str = "main"
    ) -> Document:
        url = f"{self.base_url}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        logger.info("GitHubReader fetching: %s/%s/%s @%s", owner, repo, path, ref)
        try:
            data = self._api_get(url)
            if isinstance(data, dict) and data.get("encoding") == "base64":
                content = base64.b64decode(data.get("content", "")).decode(
                    "utf-8", errors="replace"
                )
            else:
                content = json.dumps(data, indent=2, ensure_ascii=False)
            return Document(
                content=content,
                source=f"github:{owner}/{repo}/{path}@{ref}",
                metadata={
                    "reader": "github",
                    "owner": owner,
                    "repo": repo,
                    "path": path,
                    "ref": ref,
                    "sha": data.get("sha", "") if isinstance(data, dict) else "",
                    "size": data.get("size", 0) if isinstance(data, dict) else 0,
                },
            )
        except Exception as e:
            logger.error("GitHubReader failed for %s/%s/%s: %s", owner, repo, path, e)
            return Document(
                content="",
                source=f"github:{owner}/{repo}/{path}@{ref}",
                metadata={
                    "reader": "github",
                    "owner": owner,
                    "repo": repo,
                    "path": path,
                    "ref": ref,
                    "error": str(e),
                },
            )

    def read_repo_tree(
        self,
        owner: str,
        repo: str,
        path: str = "",
        ref: str = "main",
        extensions: set[str] | None = None,
    ) -> list[Document]:
        logger.info(
            "GitHubReader tree: %s/%s/%s @%s", owner, repo, path or "(root)", ref
        )
        tree_url = f"{self.base_url}/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
        try:
            data = self._api_get(tree_url)
            tree = data.get("tree", []) if isinstance(data, dict) else []
            docs = []
            code_extensions = extensions or {
                ".py",
                ".js",
                ".ts",
                ".jsx",
                ".tsx",
                ".md",
                ".txt",
                ".json",
                ".yaml",
                ".yml",
                ".toml",
                ".cfg",
                ".ini",
                ".sh",
                ".bash",
                ".go",
                ".rs",
                ".java",
                ".c",
                ".cpp",
                ".h",
                ".hpp",
                ".rb",
                ".sql",
                ".html",
                ".css",
                ".vue",
                ".svelte",
            }
            for item in tree:
                if item.get("type") != "blob":
                    continue
                item_path = item.get("path", "")
                if path and not item_path.startswith(path):
                    continue
                ext = Path(item_path).suffix.lower()
                if ext not in code_extensions:
                    continue
                doc = self.read_repo_file(owner, repo, item_path, ref)
                if not doc.metadata.get("error"):
                    docs.append(doc)
            logger.info(
                "GitHubReader tree: %d files read from %s/%s", len(docs), owner, repo
            )
            return docs
        except Exception as e:
            logger.error("GitHubReader tree failed for %s/%s: %s", owner, repo, e)
            return [
                Document(
                    content="",
                    source=f"github:{owner}/{repo}/{path}@{ref}",
                    metadata={
                        "reader": "github",
                        "owner": owner,
                        "repo": repo,
                        "error": str(e),
                    },
                )
            ]

    def _api_get(self, url: str) -> Any:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "FusionAgentStudio/1.0",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))


@dataclass
class NotionBlock:
    type: str = ""
    text: str = ""
    children: list[NotionBlock] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "text": self.text,
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotionBlock:
        return cls(
            type=data.get("type", ""),
            text=data.get("text", ""),
            children=[NotionBlock.from_dict(c) for c in data.get("children", [])],
        )


class NotionReader:
    def __init__(self, api_key: str = "", base_url: str = "https://api.notion.com/v1"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def read_page(self, page_id: str) -> Document:
        logger.info("NotionReader fetching page: %s", page_id)
        try:
            page_data = self._api_get(f"{self.base_url}/pages/{page_id}")
            title = self._extract_page_title(page_data)
            blocks = self._fetch_block_children(page_id)
            content = self._blocks_to_markdown(blocks)
            return Document(
                content=f"# {title}\n\n{content}" if title else content,
                source=f"notion:page:{page_id}",
                metadata={
                    "reader": "notion",
                    "page_id": page_id,
                    "title": title,
                    "block_count": len(blocks),
                },
            )
        except Exception as e:
            logger.error("NotionReader failed for page %s: %s", page_id, e)
            return Document(
                content="",
                source=f"notion:page:{page_id}",
                metadata={"reader": "notion", "page_id": page_id, "error": str(e)},
            )

    def read_database(self, database_id: str, max_pages: int = 50) -> list[Document]:
        logger.info(
            "NotionReader fetching database: %s (max %d)", database_id, max_pages
        )
        try:
            body = {"page_size": min(max_pages, 100)}
            result = self._api_post(
                f"{self.base_url}/databases/{database_id}/query", body
            )
            pages = result.get("results", [])
            docs = []
            for page_obj in pages[:max_pages]:
                pid = page_obj.get("id", "")
                if pid:
                    doc = self.read_page(pid)
                    docs.append(doc)
            logger.info(
                "NotionReader database: %d pages read from %s", len(docs), database_id
            )
            return docs
        except Exception as e:
            logger.error("NotionReader database failed for %s: %s", database_id, e)
            return [
                Document(
                    content="",
                    source=f"notion:database:{database_id}",
                    metadata={
                        "reader": "notion",
                        "database_id": database_id,
                        "error": str(e),
                    },
                )
            ]

    def _api_get(self, url: str) -> Any:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _api_post(self, url: str, body: dict) -> Any:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _extract_page_title(self, page_data: dict) -> str:
        props = page_data.get("properties", {})
        for prop_val in props.values():
            if prop_val.get("type") == "title":
                title_parts = prop_val.get("title", [])
                return "".join(t.get("plain_text", "") for t in title_parts)
        return ""

    def _fetch_block_children(self, block_id: str) -> list[NotionBlock]:
        blocks = []
        url = f"{self.base_url}/blocks/{block_id}/children?page_size=100"
        while url:
            data = self._api_get(url)
            for item in data.get("results", []):
                block = self._parse_block(item)
                if item.get("has_children"):
                    block.children = self._fetch_block_children(item["id"])
                blocks.append(block)
            url = data.get("next_cursor")
            if url:
                url = f"{self.base_url}/blocks/{block_id}/children?page_size=100&start_cursor={url}"
        return blocks

    def _parse_block(self, item: dict) -> NotionBlock:
        btype = item.get("type", "")
        rich_texts = item.get(btype, {}).get("rich_text", [])
        text = "".join(rt.get("plain_text", "") for rt in rich_texts)
        return NotionBlock(type=btype, text=text)

    def _blocks_to_markdown(self, blocks: list[NotionBlock]) -> str:
        lines = []
        for block in blocks:
            prefix = {
                "heading_1": "# ",
                "heading_2": "## ",
                "heading_3": "### ",
                "bulleted_list_item": "- ",
                "numbered_list_item": "1. ",
                "to_do": "- [ ] " if not block.metadata.get("checked") else "- [x] ",
                "quote": "> ",
                "code": "```\n",
                "divider": "---",
            }.get(block.type, "")
            if block.type == "code":
                lines.append(f"{prefix}{block.text}\n```")
            else:
                lines.append(f"{prefix}{block.text}")
            if block.children:
                child_md = self._blocks_to_markdown(block.children)
                for child_line in child_md.split("\n"):
                    if child_line:
                        lines.append(f"  {child_line}")
        return "\n\n".join(lines)


class PDFReader:
    def read_file(self, path: str | Path) -> Document:
        path = Path(path)
        logger.info("PDFReader reading file: %s", path)
        try:
            content = self._extract_with_pypdf(path)
            if not content:
                content = self._extract_fallback(path)
            return Document(
                content=content,
                source=str(path),
                metadata={
                    "reader": "pdf",
                    "file_name": path.name,
                    "size_bytes": path.stat().st_size,
                    "content_length": len(content),
                },
            )
        except Exception as e:
            logger.error("PDFReader failed for %s: %s", path, e)
            return Document(
                content="",
                source=str(path),
                metadata={"reader": "pdf", "file_name": path.name, "error": str(e)},
            )

    def read_bytes(self, data: bytes, source: str = "") -> Document:
        logger.info(
            "PDFReader reading bytes (%d bytes) from %s", len(data), source or "unknown"
        )
        try:
            content = self._extract_bytes_with_pypdf(data)
            if not content:
                content = data.decode("utf-8", errors="replace")
            return Document(
                content=content,
                source=source or "bytes",
                metadata={
                    "reader": "pdf",
                    "data_size": len(data),
                    "content_length": len(content),
                },
            )
        except Exception as e:
            logger.error("PDFReader bytes failed: %s", e)
            return Document(
                content="",
                source=source or "bytes",
                metadata={"reader": "pdf", "error": str(e)},
            )

    def _extract_with_pypdf(self, path: Path) -> str:
        try:
            from pypdf import PdfReader as _PdfReader

            reader = _PdfReader(str(path))
            pages = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
            logger.info("PDFReader pypdf: %d pages from %s", len(pages), path.name)
            return "\n\n".join(pages)
        except ImportError:
            try:
                from PyPDF2 import PdfReader as _PdfReader2

                reader = _PdfReader2(str(path))
                pages = []
                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append(text)
                logger.info("PDFReader PyPDF2: %d pages from %s", len(pages), path.name)
                return "\n\n".join(pages)
            except ImportError:
                logger.warning("PDFReader: neither pypdf nor PyPDF2 available")
                return ""
        except Exception as e:
            logger.warning("PDFReader pypdf extraction failed: %s", e)
            return ""

    def _extract_bytes_with_pypdf(self, data: bytes) -> str:
        try:
            from pypdf import PdfReader as _PdfReader
            import io as _io

            reader = _PdfReader(_io.BytesIO(data))
            pages = []
            for page in reader.pages:
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(text)
            return "\n\n".join(pages)
        except ImportError:
            try:
                from PyPDF2 import PdfReader as _PdfReader2
                import io as _io2

                reader = _PdfReader2(_io2.BytesIO(data))
                pages = []
                for page in reader.pages:
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append(text)
                return "\n\n".join(pages)
            except ImportError:
                return ""
        except Exception:
            return ""

    def _extract_fallback(self, path: Path) -> str:
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        logger.info("PDFReader fallback: %d chars from %s", len(text), path.name)
        return text


class DirectoryReader:
    def __init__(
        self, extensions: set[str] | None = None, exclude_dirs: set[str] | None = None
    ):
        self.extensions = extensions or SUPPORTED_EXTENSIONS
        self.exclude_dirs = exclude_dirs or {
            ".git",
            "__pycache__",
            "node_modules",
            ".venv",
            ".mypy_cache",
            ".pytest_cache",
        }

    def read_directory(self, path: str | Path, max_depth: int = 10) -> list[Document]:
        root = Path(path)
        if not root.is_dir():
            logger.error("DirectoryReader: not a directory: %s", path)
            return [
                Document(
                    content="",
                    source=str(path),
                    metadata={
                        "reader": "directory",
                        "error": f"Not a directory: {path}",
                    },
                )
            ]
        logger.info("DirectoryReader scanning: %s (max_depth=%d)", path, max_depth)
        docs = []
        doc_reader = DocumentReader()
        for file_path in self._walk(root, max_depth=max_depth):
            ext = file_path.suffix.lower()
            if ext not in self.extensions:
                continue
            try:
                if ext == ".pdf":
                    pdf_reader = PDFReader()
                    doc = pdf_reader.read_file(file_path)
                else:
                    doc = doc_reader.read_file(file_path)
                doc.metadata["reader"] = "directory"
                docs.append(doc)
            except Exception as e:
                logger.warning("DirectoryReader skipped %s: %s", file_path, e)
                docs.append(
                    Document(
                        content="",
                        source=str(file_path),
                        metadata={
                            "reader": "directory",
                            "file_name": file_path.name,
                            "error": str(e),
                        },
                    )
                )
        logger.info("DirectoryReader: %d documents from %s", len(docs), path)
        return docs

    def _walk(
        self, root: Path, max_depth: int = 10, current_depth: int = 0
    ) -> list[Path]:
        if current_depth > max_depth:
            return []
        files = []
        try:
            for entry in sorted(root.iterdir()):
                if entry.is_dir():
                    if entry.name in self.exclude_dirs or entry.name.startswith("."):
                        continue
                    files.extend(self._walk(entry, max_depth, current_depth + 1))
                elif entry.is_file():
                    files.append(entry)
        except PermissionError:
            logger.warning("DirectoryReader: permission denied: %s", root)
        return files
