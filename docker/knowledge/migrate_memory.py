"""One-time memory.md to runtime chunks migration.

Parses memory.md by ## sections and upserts each as a typed runtime chunk
in ChromaDB. Idempotent: uses upsert so re-running is safe.
"""
import os
import re
import time
import chromadb


def _slugify(text):
    """Convert a section title to a slug for chunk ID."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _map_type(title):
    """Map a memory.md section title to a chunk type."""
    lower = title.lower()
    if any(w in lower for w in ("integration", "credential", "api", "service")):
        return "integration"
    if any(w in lower for w in ("preference", "style", "verbosity")):
        return "preference"
    # projects, tools, lessons, errors, etc. -> fact
    return "fact"


def migrate(identity_dir=None, chroma_path=None):
    """Migrate memory.md sections into runtime ChromaDB chunks.

    Args:
        identity_dir: Path to identity directory (default: IDENTITY_DIR env or /data/identity).
        chroma_path: Path to ChromaDB storage (default: KNOWLEDGE_DB_PATH env or /data/knowledge/chroma).

    Returns:
        Number of chunks migrated.
    """
    if identity_dir is None:
        identity_dir = os.environ.get("IDENTITY_DIR", "/data/identity")
    if chroma_path is None:
        chroma_path = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")

    memory_path = os.path.join(identity_dir, "memory.md")
    if not os.path.exists(memory_path):
        print("[knowledge] no memory.md found, skipping migration", flush=True)
        return 0

    with open(memory_path) as f:
        content = f.read()

    # Split by ## sections (same pattern as chunker.py)
    sections = re.split(r"^## ", content, flags=re.MULTILINE)

    chunks = []
    for section in sections[1:]:  # skip content before first ##
        lines = section.strip().split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if not body:
            continue

        slug = _slugify(title)
        chunk_id = "memory.{}".format(slug)
        chunk_type = _map_type(title)

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        chunks.append({
            "id": chunk_id,
            "text": "## {}\n\n{}".format(title, body),
            "metadata": {
                "type": chunk_type,
                "source": "memory.md",
                "section": title,
                "namespace": "runtime",
                "refs": "",
                "key": chunk_id,
                "created_at": now,
                "updated_at": now,
            },
        })

    if not chunks:
        print("[knowledge] memory.md has no sections to migrate", flush=True)
        return 0

    client = chromadb.PersistentClient(path=chroma_path)
    runtime_col = client.get_or_create_collection("runtime")

    runtime_col.upsert(
        ids=[c["id"] for c in chunks],
        documents=[c["text"] for c in chunks],
        metadatas=[c["metadata"] for c in chunks],
    )

    print("[knowledge] migrated {} memory sections to runtime chunks".format(len(chunks)), flush=True)
    return len(chunks)


if __name__ == "__main__":
    migrate()
