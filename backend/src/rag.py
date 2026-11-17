"""RAG building blocks: extract → chunk → embed → Qdrant.

Pure functions, no framework coupling — the API's ingestion worker and the
agent's in-call retrieval both consume these. Embeddings go through any
OpenAI-compatible endpoint: Ollama's /v1 for the local nomic profile, or
api.openai.com for the OpenAI profile.
"""

import pathlib
import uuid

from openai import AsyncOpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    IsEmptyCondition,
    MatchValue,
    PayloadField,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)


def extract_text(path: str, ocr: str = "auto") -> str:
    """PDF/DOCX via docling; MD/TXT read directly.

    ocr (PDFs only): "auto" = docling default (OCR only where there is no
    text layer), "force" = full-page OCR for image-heavy PDFs whose content
    lives inside pictures (much slower on CPU), "off" = text layer only
    (fastest).
    """
    p = pathlib.Path(path)
    suffix = p.suffix.lower()
    if suffix in (".md", ".txt"):
        return p.read_text(errors="replace")
    if suffix in (".pdf", ".docx"):
        from docling.document_converter import DocumentConverter

        if ocr == "auto":
            converter = DocumentConverter()
        else:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import PdfFormatOption

            opts = PdfPipelineOptions()
            if ocr == "force":
                opts.do_ocr = True
                opts.ocr_options.force_full_page_ocr = True
            elif ocr == "off":
                opts.do_ocr = False
            else:
                raise ValueError(f"unknown ocr mode: {ocr}")
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=opts)
                }
            )
        result = converter.convert(path)
        return result.document.export_to_markdown()
    raise ValueError(f"unsupported format: {suffix} (PDF/DOCX/MD/TXT only)")


# Chunking geometry defaults (~500 tokens / 50 overlap). Per-agent
# overrides ride in the config as rag.chunk_size / rag.chunk_overlap —
# see api.process_doc, the only caller.
CHUNK_SIZE = 1800
CHUNK_OVERLAP = 200


def chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Fixed size + overlap (~500 tokens / 50)."""
    chunks, start = [], 0
    while start < len(text):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(piece)
        start += size - overlap
    return chunks


_embed_clients: dict[tuple[str, str], AsyncOpenAI] = {}


async def embed_texts(
    texts: list[str], *, base_url: str, api_key: str, model: str
) -> list[list[float]]:
    # Reuse one client per (base_url, api_key): a fresh client per call pays
    # TLS/connection setup every time — enough to blow the in-call RAG
    # timeout on the first turn of a session.
    client = _embed_clients.get((base_url, api_key))
    if client is None:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        _embed_clients[(base_url, api_key)] = client
    # encoding_format must be explicit: the OpenAI client defaults to
    # base64, which some OpenAI-compatible backends reject; float works
    # everywhere.
    resp = await client.embeddings.create(
        model=model, input=texts, encoding_format="float"
    )
    return [d.embedding for d in resp.data]


def qdrant(url: str) -> QdrantClient:
    return QdrantClient(url=url)


def ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
    # Idempotent, and also upgrades collections created before agent scoping.
    for field in ("doc_id", "agent_id"):
        try:
            client.create_payload_index(
                name, field, field_schema=PayloadSchemaType.KEYWORD
            )
        except Exception:
            pass


def upsert_chunks(
    client: QdrantClient,
    name: str,
    doc_id: str,
    filename: str,
    chunks: list[str],
    vectors: list[list[float]],
    agent_id: str | None = None,
) -> None:
    points = []
    for i, (c, v) in enumerate(zip(chunks, vectors)):
        payload = {
            "doc_id": doc_id,
            "filename": filename,
            "chunk_index": i,
            "text": c,
        }
        # Untagged chunks are "shared" docs, visible to every agent.
        if agent_id:
            payload["agent_id"] = agent_id
        points.append(PointStruct(id=str(uuid.uuid4()), vector=v, payload=payload))
    client.upsert(name, points=points)


def search(
    client: QdrantClient,
    name: str,
    vector: list[float],
    top_k: int,
    agent_id: str | None = None,
) -> list[dict]:
    # Agent scoping: an agent retrieves its own docs OR untagged (shared)
    # docs. No agent_id = no filter (search everything).
    query_filter = None
    if agent_id:
        query_filter = Filter(
            should=[
                FieldCondition(key="agent_id", match=MatchValue(value=agent_id)),
                IsEmptyCondition(is_empty=PayloadField(key="agent_id")),
            ]
        )
    hits = client.query_points(
        name, query=vector, limit=top_k, query_filter=query_filter
    ).points
    return [h.payload | {"score": h.score} for h in hits]


def delete_doc(client: QdrantClient, name: str, doc_id: str) -> None:
    client.delete(
        name,
        points_selector=FilterSelector(
            filter=Filter(
                must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
            )
        ),
    )
