"""Deploy-time re-indexer for the system namespace.

Reads LOCKED markdown files (system.md, onboarding.md, schemas/*.schema.md),
chunks them, and indexes into ChromaDB's "system" collection.
"""
import os
import time
import logging
import chromadb
from knowledge.chunker import chunk_file

logger = logging.getLogger("knowledge.indexer")


def run_index(client=None, identity_dir=None):
    """Run the indexing pipeline.

    Args:
        client: ChromaDB client (defaults to PersistentClient).
        identity_dir: Path to identity files (defaults to IDENTITY_DIR env).
    """
    if identity_dir is None:
        identity_dir = os.environ.get("IDENTITY_DIR", "/data/identity")
    if client is None:
        chroma_path = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")
        client = chromadb.PersistentClient(path=chroma_path)

    # wipe system collection (clean rebuild)
    try:
        client.delete_collection("system")
    except Exception:
        pass  # collection doesn't exist yet
    system_col = client.create_collection("system", metadata={"hnsw:space": "cosine"})

    # chunk LOCKED files
    chunks = []

    system_md = os.path.join(identity_dir, "system.md")
    if os.path.exists(system_md):
        chunks.extend(chunk_file(system_md, "system.md"))

    onboarding_md = os.path.join(identity_dir, "onboarding.md")
    if os.path.exists(onboarding_md):
        chunks.extend(chunk_file(onboarding_md, "onboarding.md"))

    schemas_dir = os.path.join(identity_dir, "schemas")
    if os.path.isdir(schemas_dir):
        for fname in sorted(os.listdir(schemas_dir)):
            if fname.endswith(".schema.md"):
                path = os.path.join(schemas_dir, fname)
                chunks.extend(chunk_file(path, "schemas/{}".format(fname)))

    if chunks:
        # stamp all system chunks with the deploy time
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for c in chunks:
            c["metadata"]["created_at"] = now
            c["metadata"]["updated_at"] = now

        try:
            system_col.add(
                ids=[c["id"] for c in chunks],
                documents=[c["text"] for c in chunks],
                metadatas=[c["metadata"] for c in chunks],
            )
        except Exception as e:
            logger.error("failed to index system chunks: %s", e)
            return 0

    print("[knowledge] indexed {} system chunks".format(len(chunks)), flush=True)
    return len(chunks)


def main():
    """Entry point for deploy-time indexing."""
    count = run_index()
    return count


if __name__ == "__main__":
    main()
