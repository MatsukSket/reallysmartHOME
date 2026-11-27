"""Публикация версий локального индекса после успешной сборки."""
import os
import re
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from .corpus import parse_manual, validate_name

MANUALS = Path(__file__).resolve().parents[2] / "data" / "manuals"
ROOT = Path(os.getenv("RAG_ROOT", Path(__file__).resolve().parents[3] / "runtime" / "rag"))
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
BUILD_LOCK = threading.Lock()


def active_version(root=None):
    root = ROOT if root is None else root
    pointer = root / "CURRENT"
    if not pointer.is_file():
        return None
    version = pointer.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", version):
        raise ValueError("Некорректный указатель CURRENT")
    return root / version


def uploaded_texts(root=None):
    version = active_version(root)
    if version is None:
        return {}
    return {path.name: path.read_text(encoding="utf-8-sig") for path in (version / "uploads").glob("*.md")}


def passages():
    for name, text in documents(uploaded_texts()).items():
        yield from parse_manual(name, text)


def documents(uploads):
    builtins = {path.name: path.read_text(encoding="utf-8-sig") for path in MANUALS.glob("*.md")}
    return dict(sorted((builtins | uploads).items()))


@lru_cache(maxsize=1)
def encoder():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL, local_files_only=os.getenv("RAG_DOWNLOAD") != "1")


def create_index(upload=None):
    with BUILD_LOCK:
        uploads = uploaded_texts()
        if upload:
            name, text = upload
            validate_name(name)
            if name.casefold() in {filename.casefold() for filename in documents(uploads)}:
                raise FileExistsError("Документ с таким именем уже есть")
            uploads[name] = text

        chunks = [chunk for name, text in documents(uploads).items() for chunk in parse_manual(name, text)]
        if not chunks:
            raise ValueError("Нет руководств")
        import chromadb

        vectors = encoder().encode([chunk["text"] for chunk in chunks], normalize_embeddings=True)
        version = uuid.uuid4().hex
        destination = ROOT / version
        destination.mkdir(parents=True)
        collection = chromadb.PersistentClient(path=str(destination)).create_collection(
            "manuals", configuration={"hnsw": {"space": "cosine"}}, metadata={"distance": "cosine"},
        )
        collection.add(ids=[chunk["chunk_id"] for chunk in chunks], documents=[chunk["text"] for chunk in chunks],
                       metadatas=[{k: v for k, v in chunk.items() if k != "text"} for chunk in chunks],
                       embeddings=vectors.tolist())
        if uploads:
            folder = destination / "uploads"
            folder.mkdir()
            for name, text in uploads.items():
                (folder / name).write_text(text, encoding="utf8")
        pointer = ROOT / (version + ".txt")
        pointer.write_text(version, encoding="ascii")
        pointer.replace(ROOT / "CURRENT")
        return version


if __name__ == "__main__":
    print(create_index())
