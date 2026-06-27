"""Builds the handbook RAG store. The real handbook text is markdown
(`handbook.md`, recovered from the base64-embedded content in handbook.html).
Run this once (or after editing handbook.md):  python vector_store.py
"""
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

load_dotenv()

# Embedding model must match the one used at query time (see tools.py).
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SOURCE = "handbook.md"


def create_vector_db():
    text = open(SOURCE, encoding="utf-8").read()

    # Markdown-aware splitting: prefer to break at headings, then blank lines.
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1500,
        chunk_overlap=200,
        separators=["\n## ", "\n### ", "\n#### ", "\n\n", "\n", " ", ""],
    )
    chunks = splitter.create_documents([text])

    Chroma.from_documents(
        chunks,
        HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL),
        persist_directory="db",
    )
    print(f"Vector DB created with {len(chunks)} chunks from {SOURCE}.")


if __name__ == "__main__":
    create_vector_db()
