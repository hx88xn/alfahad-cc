import os
import glob
import time
from pinecone import Pinecone, ServerlessSpec
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from dotenv import load_dotenv
import uuid

load_dotenv(override=True)

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "alfardan-callcenter")
NAMESPACE = os.getenv("PINECONE_NAMESPACE", "alfardan-data")

pc = Pinecone(api_key=PINECONE_API_KEY)

existing_indexes = [i["name"] for i in pc.list_indexes()]
if INDEX_NAME not in existing_indexes:
    print(f"📦 Creating Pinecone index '{INDEX_NAME}'...")
    pc.create_index(
        name=INDEX_NAME,
        dimension=1024,
        metric="cosine",
        spec=ServerlessSpec(
            cloud="aws",
            region="us-east-1"
        )
    )
    print("⏳ Waiting for index to be ready...")
    while not pc.describe_index(INDEX_NAME).status["ready"]:
        time.sleep(1)
    print(f"✅ Index '{INDEX_NAME}' created successfully!")

index = pc.Index(INDEX_NAME)

embeddings = OpenAIEmbeddings(model="text-embedding-3-small", dimensions=1024)


def get_source_category(filename: str) -> dict:
    name = os.path.basename(filename).replace(".txt", "")

    if name.startswith("al-fardan") or name.startswith("alfardan"):
        category = "Services"
        rest = name.replace("al-fardan-", "").replace("alfardan-", "").replace("al-fardan", "").replace("alfardan", "").strip("-_")
        subcategory = rest.replace("-", " ").replace("_", " ").title() if rest else "Al Fardan"
    elif name.startswith("en_") or name == "en":
        category = "Public Website (English)"
        subcategory = name.replace("en_", "").replace("_", " ").title() if name != "en" else "Home (EN)"
    elif name == "ekyc":
        category = "eKYC"
        subcategory = "Electronic KYC"
    elif name == "index":
        category = "Site"
        subcategory = "Index"
    elif name == "search":
        category = "Site"
        subcategory = "Search"
    else:
        category = "General"
        subcategory = name.replace("_", " ").title()

    return {
        "category": category,
        "subcategory": subcategory,
        "source_file": name
    }


def ingest_text_file(file_path: str):
    print(f"📄 Ingesting {file_path}...")

    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    if not text.strip():
        print(f"⚠️ Skipping empty file: {file_path}")
        return

    source_info = get_source_category(file_path)

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=150,
        length_function=len,
        is_separator_regex=False,
        separators=["\n\n", "\n", ". ", " ", ""]
    )

    chunks = text_splitter.split_text(text)

    vectors = []
    for i, chunk in enumerate(chunks):
        doc_id = str(uuid.uuid4())
        vector = embeddings.embed_query(chunk)

        metadata = {
            "text": chunk,
            "category": source_info["category"],
            "subcategory": source_info["subcategory"],
            "source_file": source_info["source_file"],
            "chunk_index": i,
            "total_chunks": len(chunks)
        }

        vectors.append({
            "id": doc_id,
            "values": vector,
            "metadata": metadata
        })

        if len(vectors) >= 50:
            index.upsert(vectors=vectors, namespace=NAMESPACE)
            print(f"  ✓ Upserted batch of {len(vectors)} vectors")
            vectors = []

    if vectors:
        index.upsert(vectors=vectors, namespace=NAMESPACE)
        print(f"  ✓ Upserted final batch of {len(vectors)} vectors")

    print(f"✅ Completed: {file_path} ({len(chunks)} chunks)")


def ingest_all_pages(pages_dir: str = "pages"):
    txt_files = glob.glob(os.path.join(pages_dir, "*.txt"))

    if not txt_files:
        print(f"❌ No .txt files found in {pages_dir}")
        return

    print(f"\n🚀 Starting ingestion of {len(txt_files)} files into namespace '{NAMESPACE}'...\n")

    for file_path in sorted(txt_files):
        try:
            ingest_text_file(file_path)
        except Exception as e:
            print(f"❌ Error processing {file_path}: {e}")

    print(f"\n✅ Ingestion complete! All files indexed in namespace '{NAMESPACE}'")

    stats = index.describe_index_stats()
    if NAMESPACE in stats.get("namespaces", {}):
        count = stats["namespaces"][NAMESPACE]["vector_count"]
        print(f"📊 Total vectors in namespace: {count}")


def clear_namespace():
    print(f"🗑️ Clearing namespace '{NAMESPACE}'...")
    index.delete(delete_all=True, namespace=NAMESPACE)
    print(f"✅ Namespace '{NAMESPACE}' cleared")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--clear":
        clear_namespace()

    ingest_all_pages("pages")
