import os
import shutil
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_community.vectorstores import Chroma

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANUALS_DIR = os.path.join(BASE_DIR, "data", "manuals")
DB_DIR = os.path.join(BASE_DIR, "data", "chroma_db")

def create_index():
    print(f"Starting indexing process from: {MANUALS_DIR}")
    
    loader = DirectoryLoader(
        MANUALS_DIR, 
        glob="**/*.md", 
        loader_cls=TextLoader,
        loader_kwargs={'encoding': 'utf-8'}
    )
    documents = loader.load()
    
    if not documents:
        print("Error: No .md files found in the specified directory.")
        return

    print(f"Loaded {len(documents)} document(s).")

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=100,
        separators=["\n\n", "\n", ".", " "]
    )
    chunks = text_splitter.split_documents(documents)
    print(f"Split documents into {len(chunks)} chunk(s).")

    print("Loading embedding model...")
    embeddings_model = HuggingFaceEmbeddings(
        model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )

    if os.path.exists(DB_DIR):
        print(f"Removing existing database at: {DB_DIR}")
        shutil.rmtree(DB_DIR)

    print(f"Saving vector database to: {DB_DIR}")
    Chroma.from_documents(
        documents=chunks,
        embedding=embeddings_model,
        persist_directory=DB_DIR
    )
    print("Indexing completed successfully.")

if __name__ == "__main__":
    create_index()