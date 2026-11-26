"""Версионированный локальный индекс. Предыдущие версии сохраняются."""
import os
import re
import uuid
from pathlib import Path
from functools import lru_cache

MANUALS = Path(__file__).resolve().parents[2]/"data"/"manuals"
ROOT = Path(os.getenv("RAG_ROOT", Path(__file__).resolve().parents[3]/"runtime"/"rag"))
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

def passages():
    for path in sorted(MANUALS.glob("*.md")):
        for i,part in enumerate(re.split(r"\n\s*\n",path.read_text(encoding="utf8"))):
            code = re.search(r"\bE\d{2}\b",part)
            yield {"text":part.strip(),"source":path.name,"chunk_id":f"{path.stem}-{i}",
                   "model":"Baxi Eco 4s" if path.stem=="boiler_baxi" else "",
                   "code":code.group() if code else ""}

@lru_cache(maxsize=1)
def encoder():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL,local_files_only=os.getenv("RAG_DOWNLOAD")!="1")

def create_index():
    import chromadb
    chunks=list(passages())
    if not chunks: raise ValueError("Нет руководств")
    vectors=encoder().encode([c["text"] for c in chunks],normalize_embeddings=True)
    version=uuid.uuid4().hex
    collection=chromadb.PersistentClient(path=str(ROOT/version)).create_collection("manuals")
    collection.add(ids=[c["chunk_id"] for c in chunks],documents=[c["text"] for c in chunks],
        metadatas=[{k:v for k,v in c.items() if k!="text"} for c in chunks],embeddings=vectors.tolist())
    pointer=ROOT/(version+".txt")
    pointer.write_text(version)
    pointer.replace(ROOT/"CURRENT")
    return version

if __name__=="__main__": print(create_index())
