from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

import yaml


class _MainTextExtractor(HTMLParser):
    _BLOCK_TAGS = {"article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "pre", "section", "table", "tr"}
    _SKIP_TAGS = {"a", "button", "nav", "script", "style", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._main_depth = 0
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "main":
            self._main_depth += 1
            return
        if not self._main_depth:
            return
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "main" and self._main_depth:
            self._main_depth -= 1
            return
        if not self._main_depth:
            return
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._main_depth and not self._skip_depth:
            text = " ".join(data.split())
            if text:
                self._parts.append(text)

    def text(self) -> str:
        output: list[str] = []
        previous_newline = False
        for part in self._parts:
            if part == "\n":
                if not previous_newline:
                    output.append("\n")
                previous_newline = True
            else:
                if output and not previous_newline:
                    output.append(" ")
                output.append(part)
                previous_newline = False
        return "".join(output).strip() + "\n"


def build_bundle(
    catalog_path: Path,
    output_dir: Path,
    source_revision: str,
    fetch: Callable[[str], str] | None = None,
) -> None:
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not catalog.get("documents"):
        raise ValueError("catalog must define a non-empty documents list")
    if not source_revision.strip():
        raise ValueError("source_revision must not be empty")

    fetcher = fetch or _fetch_page
    output_dir.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, str]] = []
    for item in catalog["documents"]:
        html = fetcher(item["canonical_url"])
        content = _extract_main_text(html)
        if not content.strip():
            raise ValueError(f"could not extract main content for {item['id']}")
        filename = f"{item['id']}.txt"
        (output_dir / filename).write_text(content, encoding="utf-8")
        documents.append(
            {
                "id": item["id"],
                "title": item["title"],
                "canonical_url": item["canonical_url"],
                "source_path": item["source_path"],
                "source_revision": source_revision,
                "file": filename,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        )

    manifest = {
        "schema_version": 1,
        "kubernetes_version": catalog["version"],
        "source_repository": catalog["source_repository"],
        "imported_at": datetime.now(UTC).isoformat(),
        "license": "CC BY 4.0",
        "documents": documents,
        "rules": catalog.get("rules", {}),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "NOTICE").write_text(
        "Kubernetes documentation in this bundle is imported from kubernetes.io.\n"
        "Copyright The Kubernetes Authors. Licensed under CC BY 4.0.\n"
        "Each document's manifest entry records its canonical source and upstream revision.\n",
        encoding="utf-8",
    )


def _fetch_page(url: str) -> str:
    request = Request(url, headers={"User-Agent": "kcp-docs-sync/0.1"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def _extract_main_text(html: str) -> str:
    extractor = _MainTextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local Kubernetes documentation bundle")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    build_bundle(args.catalog, args.output, args.source_revision)


if __name__ == "__main__":
    main()
