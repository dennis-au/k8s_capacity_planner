from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from markupsafe import Markup


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    canonical_url: str
    source_path: str
    source_revision: str
    sha256: str
    content: str


@dataclass(frozen=True)
class DocumentHeading:
    anchor: str
    title: str


@dataclass(frozen=True)
class RenderedDocument:
    content: Markup
    headings: tuple[DocumentHeading, ...]


def render_document(content: str, title: str) -> RenderedDocument:
    """Turn the verified, local text bundle into readable, escaped article markup."""
    blocks: list[str] = []
    headings: list[DocumentHeading] = []
    list_items: list[str] = []
    used_anchors: dict[str, int] = {}

    def flush_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{escape(item)}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    lines = content.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            flush_list()
            index += 1
            continue
        if line == title:
            flush_list()
            index += 1
            continue
        if _is_flowchart_start(line):
            flush_list()
            source, index = _consume_flowchart(lines, index)
            blocks.append(_render_flowchart(source))
            continue
        if _is_feature_state(line):
            flush_list()
            blocks.append(f'<p class="doc-feature-state">{escape(line)}</p>')
            index += 1
            continue
        if _is_heading(line):
            flush_list()
            anchor = _heading_anchor(line, used_anchors)
            headings.append(DocumentHeading(anchor=anchor, title=line))
            blocks.append(f'<h3 id="{anchor}">{escape(line)}</h3>')
            index += 1
            continue
        if _is_list_item(line):
            list_items.append(line)
            index += 1
            continue
        flush_list()
        if _is_manifest_line(line):
            blocks.append(f"<pre><code>{escape(line)}</code></pre>")
        elif line == "Note:":
            blocks.append('<p class="doc-note-label">Note</p>')
        else:
            blocks.append(f"<p>{escape(line)}</p>")
        index += 1

    flush_list()
    return RenderedDocument(content=Markup("\n".join(blocks)), headings=tuple(headings))


def _is_heading(line: str) -> bool:
    if len(line) > 88 or line[-1:] in {".", ":"} or not line[0].isupper():
        return False
    words = line.replace("'", "").split()
    return 1 <= len(words) <= 10


def _is_list_item(line: str) -> bool:
    return line.startswith(("Enforce ", "Set ", "Define "))


def _is_manifest_line(line: str) -> bool:
    return line.startswith(("apiVersion :", "apiVersion:"))


def _is_flowchart_start(line: str) -> bool:
    return line.startswith(("graph ", "flowchart "))


def _is_feature_state(line: str) -> bool:
    return line.startswith("FEATURE STATE:")


def _consume_flowchart(lines: list[str], start: int) -> tuple[str, int]:
    parts = [lines[start].strip()]
    index = start + 1
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("Figure ") or (_is_heading(line) and not _is_flowchart_syntax(line)):
            break
        if line:
            parts.append(line)
        index += 1
    return " ".join(parts), index


def _is_flowchart_syntax(line: str) -> bool:
    return any(token in line for token in ("[", "]", "-->", "-.->", "|", "subgraph", "class", "direction"))


_NODE_PATTERN = re.compile(r"\b([A-Za-z][\w-]*)\[([^\]]+)\]")
_EDGE_PATTERN = re.compile(
    r"\b([A-Za-z][\w-]*)\s*(--+>|-\.+->)\s*(?:\|([^|]+)\|\s*)?([A-Za-z][\w-]*)\b"
)
_SUBGRAPH_PATTERN = re.compile(r"\bsubgraph\s+([A-Za-z][\w-]*)\[([^\]]+)\]\s+(.*?)\s+\bend\b")


def _render_flowchart(source: str) -> str:
    normalized = " ".join(source.split())
    nodes = {identifier: " ".join(label.split()) for identifier, label in _NODE_PATTERN.findall(normalized)}
    groups: dict[str, str] = {}
    for _, group_label, members in _SUBGRAPH_PATTERN.findall(normalized):
        for member in re.findall(r"\b[A-Za-z][\w-]*\b", members):
            if member in nodes:
                groups[member] = " ".join(group_label.split())

    diagram = re.split(r"\bclassDef\b|\bclass\s", normalized, maxsplit=1)[0]
    diagram = _NODE_PATTERN.sub(lambda match: match.group(1), diagram)
    relationships: list[str] = []
    summary: list[str] = []
    for source_id, arrow, label, target_id in _EDGE_PATTERN.findall(diagram):
        source_label = nodes.get(source_id, source_id)
        target_label = nodes.get(target_id, target_id)
        relation = " ".join(label.split()) if label else ("updates" if "." in arrow else "controls")
        relationship_class = " doc-diagram-relationship-dashed" if "." in arrow else ""
        relationships.append(
            f'<li class="doc-diagram-relationship{relationship_class}">'
            f'{_diagram_node(source_label, groups.get(source_id))}'
            f'<span class="doc-diagram-connector"><span aria-hidden="true">→</span>{escape(relation)}</span>'
            f'{_diagram_node(target_label, groups.get(target_id))}'
            "</li>"
        )
        summary.append(f"{source_label} {relation} {target_label}.")

    if not relationships:
        return '<p class="doc-diagram-unavailable">This bundled diagram could not be rendered as relationships.</p>'
    description = " ".join(summary)
    return (
        f'<figure class="doc-diagram" aria-label="{escape(description)}">'
        "<figcaption>Architecture diagram</figcaption>"
        f'<ol class="doc-diagram-relationships">{"".join(relationships)}</ol>'
        f'<p class="doc-diagram-summary">{escape(description)}</p>'
        "</figure>"
    )


def _diagram_node(label: str, group: str | None) -> str:
    group_label = f'<small>{escape(group)}</small>' if group else ""
    return f'<span class="doc-diagram-node">{group_label}{escape(label)}</span>'


def _heading_anchor(title: str, used_anchors: dict[str, int]) -> str:
    base = "".join(character.lower() if character.isalnum() else "-" for character in title).strip("-")
    base = "-".join(part for part in base.split("-") if part) or "section"
    count = used_anchors.get(base, 0) + 1
    used_anchors[base] = count
    return base if count == 1 else f"{base}-{count}"


class DocumentRegistry:
    def __init__(self, bundle_dir: Path) -> None:
        self.bundle_dir = bundle_dir
        manifest_path = bundle_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"documentation manifest not found: {manifest_path}")
        self.manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        self._documents = self._load_documents()

    @property
    def kubernetes_version(self) -> str:
        return str(self.manifest["kubernetes_version"])

    def list(self) -> list[Document]:
        return sorted(self._documents.values(), key=lambda document: document.title)

    def get(self, document_id: str) -> Document:
        try:
            return self._documents[document_id]
        except KeyError as exc:
            raise KeyError(f"unknown documentation id: {document_id}") from exc

    def search(self, query: str) -> list[Document]:
        normalized = query.strip().lower()
        if not normalized:
            return self.list()
        return [
            document
            for document in self.list()
            if normalized in document.title.lower() or normalized in document.content.lower()
        ]

    def source_for_rule(self, rule_id: str) -> dict[str, str]:
        rules = self.manifest.get("rules", {})
        source = rules.get(rule_id)
        if not source:
            raise KeyError(f"no documentation source registered for rule: {rule_id}")
        document = self.get(source["document_id"])
        return {
            "document_id": document.id,
            "document_title": document.title,
            "section": source["section"],
            "canonical_url": document.canonical_url,
            "kubernetes_version": self.kubernetes_version,
        }

    def _load_documents(self) -> dict[str, Document]:
        documents: dict[str, Document] = {}
        for metadata in self.manifest.get("documents", []):
            content_path = self.bundle_dir / metadata["file"]
            content = content_path.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if digest != metadata["sha256"]:
                raise ValueError(f"documentation checksum mismatch: {metadata['id']}")
            documents[metadata["id"]] = Document(
                id=metadata["id"],
                title=metadata["title"],
                canonical_url=metadata["canonical_url"],
                source_path=metadata["source_path"],
                source_revision=metadata["source_revision"],
                sha256=digest,
                content=content,
            )
        return documents
