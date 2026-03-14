"""Markdown-to-chunks splitter with type inference for the knowledge base."""
import re


def chunk_file(filepath, source_name):
    """Split a markdown file into chunks by ## and ### sections.

    Args:
        filepath: Path to the markdown file.
        source_name: Human-readable source identifier (e.g. "system.md").

    Returns:
        List of chunk dicts with id, text, and metadata.
    """
    with open(filepath) as f:
        content = f.read()

    chunks = []
    sections = re.split(r"^## ", content, flags=re.MULTILINE)

    for section in sections[1:]:  # skip content before first ##
        lines = section.strip().split("\n")
        section_title = lines[0].strip()
        section_body = "\n".join(lines[1:]).strip()

        # check for ### subsections
        subsections = re.split(r"^### ", section_body, flags=re.MULTILINE)

        if len(subsections) > 1:
            # intro text before first ###
            intro = subsections[0].strip()
            if intro:
                chunk_id = _make_id(source_name, section_title)
                chunks.append({
                    "id": chunk_id,
                    "text": "## {}\n\n{}".format(section_title, intro),
                    "metadata": {
                        "type": _infer_type(section_title),
                        "source": source_name,
                        "section": section_title,
                        "namespace": "system",
                        "refs": "",
                        "key": chunk_id,
                    },
                })

            for sub in subsections[1:]:
                sub_lines = sub.strip().split("\n")
                sub_title = sub_lines[0].strip()
                sub_body = "\n".join(sub_lines[1:]).strip()
                chunk_id = _make_id(source_name, section_title, sub_title)
                chunks.append({
                    "id": chunk_id,
                    "text": "## {} > {}\n\n{}".format(section_title, sub_title, sub_body),
                    "metadata": {
                        "type": _infer_type(section_title, sub_title),
                        "source": source_name,
                        "section": "{} > {}".format(section_title, sub_title),
                        "namespace": "system",
                        "refs": "",
                        "key": chunk_id,
                    },
                })
        else:
            # no subsections, entire ## section is one chunk
            chunk_id = _make_id(source_name, section_title)
            chunks.append({
                "id": chunk_id,
                "text": "## {}\n\n{}".format(section_title, section_body),
                "metadata": {
                    "type": _infer_type(section_title),
                    "source": source_name,
                    "section": section_title,
                    "namespace": "system",
                    "refs": "",
                    "key": chunk_id,
                },
            })

    return chunks


def _make_id(source, *parts):
    """Generate hierarchical dot-notation ID from source and section parts.

    Example: _make_id("system.md", "Platform", "Architecture")
             -> "system.platform.architecture"
    """
    base = source.replace(".md", "").replace("/", ".").replace(".schema", "")
    slug_parts = [re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") for p in parts]
    return "{}.{}".format(base, ".".join(slug_parts))


def _infer_type(section, subsection=""):
    """Infer chunk type tag from section/subsection names.

    Returns one of: procedure, schema, fact, preference, integration.
    """
    combined = "{} {}".format(section, subsection).lower()
    if any(w in combined for w in ("rule", "protocol", "defense", "protection", "hygiene")):
        return "procedure"
    if any(w in combined for w in ("schema", "format")):
        return "schema"
    if any(w in combined for w in ("platform", "architecture", "extension", "endpoint")):
        return "fact"
    if any(w in combined for w in ("preference", "verbosity", "style")):
        return "preference"
    if any(w in combined for w in ("integration", "credential", "vault")):
        return "integration"
    return "procedure"  # default: most system.md content is procedural
