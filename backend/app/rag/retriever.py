import os
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

embeddings_model = HuggingFaceEmbeddings(
    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

def get_context(query: str, k: int = 3) -> str:
    """
    Выполняет семантический поиск по ChromaDB и возвращает объединенный текст чанков.
    
    :param query: Вопрос пользователя на естественном языке
    :param k: Количество возвращаемых релевантных фрагментов
    :return: Форматированная строка контекста для передачи в LLM
    """
    if not os.path.exists(DB_DIR):
        raise FileNotFoundError(f"Database directory not found at {DB_DIR}. Run indexer.py first.")

    vector_db = Chroma(
        persist_directory=DB_DIR,
        embedding_function=embeddings_model
    )

    results = vector_db.similarity_search(query, k=k)

    if not results:
        return "No relevant documentation found."

    formatted_chunks = []
    for i, doc in enumerate(results, start=1):
        source = doc.metadata.get("source", "unknown")
        source_filename = os.path.basename(source)
        chunk_entry = f"[Document {i} | Source: {source_filename}]\n{doc.page_content.strip()}"
        formatted_chunks.append(chunk_entry)

    return "\n\n---\n\n".join(formatted_chunks)

if __name__ == "__main__":
    print("Testing Retriever module. Type 'exit' to quit.")
    while True:
        user_query = input("\nEnter query > ")
        if user_query.strip().lower() in ["exit", "quit", "q"]:
            break
        
        context = get_context(user_query)
        print("\n=== RETRIEVED CONTEXT ===")
        print(context)
        print("=========================")