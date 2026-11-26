import re
from .indexer import ROOT, encoder, passages

class Knowledge:
    def __init__(self, vector=False): self.vector=vector
    def search(self,query,limit=5):
        if self.vector:
            import chromadb
            version=(ROOT/"CURRENT").read_text().strip()
            coll=chromadb.PersistentClient(path=str(ROOT/version)).get_collection("manuals")
            codes=re.findall(r"\bE\d{2}\b",query.upper())
            opts={"where":{"code":codes[0]}} if codes else {}
            r=coll.query(query_embeddings=encoder().encode([query],normalize_embeddings=True).tolist(),
                         n_results=limit,**opts)
            return [dict(meta,text=t) for meta,t,dist in zip(r["metadatas"][0],r["documents"][0],r["distances"][0])
                    if codes or 1.0-dist/2 >= 0.35]
        words=set(re.findall(r"\w+",query.lower()))
        docs=list(passages())
        def score(d):
            return len(words.intersection(re.findall(r"\w+",d["text"].lower()))) + 10*(d["code"].lower() in words)
        return [d for d in sorted(docs,key=score,reverse=True) if score(d)>0][:limit]

def get_context(query,k=3):
    return "\n\n".join(d["text"] for d in Knowledge(True).search(query,k))
