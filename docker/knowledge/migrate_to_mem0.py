"""One-time migration: ChromaDB runtime collection -> mem0.

Reads all entries from the chromadb "runtime" collection and stores each
in mem0 via add(infer=False) — zero LLM calls, local embeddings only.
A sentinel file prevents re-runs on subsequent boots.
"""
import os
import sys
import time

# Disable telemetry before any mem0 import
os.environ["MEM0_TELEMETRY"] = "false"

CHROMA_PATH = os.environ.get("KNOWLEDGE_DB_PATH", "/data/knowledge/chroma")
SENTINEL = os.path.join(os.path.dirname(CHROMA_PATH), ".mem0_migrated")


def migrate(chroma_path=None, sentinel_path=None):
    """Migrate runtime ChromaDB entries to mem0. One-time, idempotent.

    Args:
        chroma_path: Path to ChromaDB storage (default: KNOWLEDGE_DB_PATH env).
        sentinel_path: Path to sentinel file (default: sibling of chroma_path).

    Returns:
        Number of entries successfully migrated.
    """
    if chroma_path is None:
        chroma_path = CHROMA_PATH
    if sentinel_path is None:
        sentinel_path = SENTINEL

    # Guard: already migrated
    if os.path.exists(sentinel_path):
        print("[mem0-migrate] already migrated, skipping", flush=True)
        return 0

    # Read source: chromadb runtime collection
    import chromadb

    client = chromadb.PersistentClient(path=chroma_path)
    try:
        runtime_col = client.get_collection("runtime")
    except Exception:
        print("[mem0-migrate] no runtime collection found, nothing to migrate", flush=True)
        _touch_sentinel(sentinel_path)
        return 0

    all_data = runtime_col.get(include=["documents", "metadatas"])
    if not all_data["ids"]:
        print("[mem0-migrate] runtime collection is empty, nothing to migrate", flush=True)
        _touch_sentinel(sentinel_path)
        return 0

    # Initialize mem0
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from mem0 import Memory
    from mem0_config import build_mem0_config

    config = build_mem0_config()
    memory = Memory.from_config(config)
    user_id = os.environ.get("MEM0_USER_ID", "default")

    # Migrate each entry
    migrated = 0
    for i, doc_id in enumerate(all_data["ids"]):
        doc = all_data["documents"][i] if all_data["documents"] else ""
        meta = all_data["metadatas"][i] if all_data["metadatas"] else {}

        if not doc or not doc.strip():
            continue

        try:
            # infer=False: bypass LLM extraction, store content directly
            memory.add(
                messages=doc,
                user_id=user_id,
                infer=False,
                metadata={
                    "source": "chromadb_migration",
                    "original_key": doc_id,
                    "original_type": meta.get("type", ""),
                },
            )
            migrated += 1
        except Exception as e:
            print(f"[mem0-migrate] failed to migrate {doc_id}: {e}", flush=True)

    _touch_sentinel(sentinel_path)
    print(f"[mem0-migrate] migrated {migrated}/{len(all_data['ids'])} entries", flush=True)
    return migrated


def _touch_sentinel(path):
    """Create sentinel file to prevent re-migration."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"migrated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")


if __name__ == "__main__":
    migrate()
