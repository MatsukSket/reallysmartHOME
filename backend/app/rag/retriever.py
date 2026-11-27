"""Search by explicit metadata or by similarity with an abstention threshold."""
import math
import os
import re

from .indexer import ROOT, encoder, passages

_STOP_WORDS = set("а и или в во на по как что это где когда какая какой какие сколько для при ли можно мне у из от с о об чем к не работает".split())


class Knowledge:
    def __init__(self, vector=False):
        self.vector = vector

    def search(self, query, limit=5, *, code=None, source=None, device_type=None, model=None):
        filters = [{"code": code or ""}]
        if source:
            filters.append({"source": source})
        for key, value in (("device_type", device_type), ("model", model)):
            if value:
                filters.append({key: value})
        where = filters[0] if len(filters) == 1 else {"$and": filters}
        query_words = keywords(query)
        if source or device_type:
            query_words -= {"конди", "котел", "котла", "котле", "котлу", "baxi"}
        if self.vector:
            import chromadb

            version = (ROOT / "CURRENT").read_text().strip()
            collection = chromadb.PersistentClient(path=str(ROOT / version)).get_collection("manuals")
            if code:
                result = collection.get(where=where, limit=limit, include=["metadatas", "documents"])
                return [dict(meta, text=text, match="code") for meta, text in zip(result["metadatas"], result["documents"])]
            minimum = float(os.getenv("RAG_MIN_SIMILARITY", "0.50"))
            if not math.isfinite(minimum) or not 0 <= minimum <= 1:
                raise ValueError("RAG_MIN_SIMILARITY должен быть от 0 до 1")
            result = collection.query(
                query_embeddings=encoder().encode([query], normalize_embeddings=True).tolist(),
                n_results=max(limit, 8), where=where, include=["metadatas", "documents", "distances"],
            )
            cosine = (getattr(collection, "metadata", None) or {}).get("distance") == "cosine"
            hits = []
            for meta, text, distance in zip(result["metadatas"][0], result["documents"][0], result["distances"][0]):
                similarity = 1 - distance if cosine else 1 - distance / 2
                if similarity >= minimum and (similarity >= 0.65 or query_words & keywords(text)):
                    hits.append(dict(meta, text=text, similarity=round(similarity, 4)))
            return hits[:limit]

        docs = [doc for doc in passages() if all(doc.get(key) == value for item in filters for key, value in item.items())]
        if code:
            return docs[:limit]
        ranked = [(len(query_words & keywords(doc["text"])), doc) for doc in docs]
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [doc for score, doc in ranked if score >= (1 if source or device_type else 2)][:limit]


def keywords(text):
    tokens = re.findall(r"[\w]+", text.casefold().replace("ё", "е"))
    return {word[:5] for word in tokens if len(word) > 2 and word not in _STOP_WORDS}


def get_context(query, k=3):
    return "\n\n".join(doc["text"] for doc in Knowledge(True).search(query, k))
