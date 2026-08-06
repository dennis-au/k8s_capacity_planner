from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    canonical_url: str
    source_path: str
    source_revision: str
    sha256: str
    content: str


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
