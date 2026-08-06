from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kcp.docs import DocumentRegistry
from kcp.docs_sync import build_bundle


class DocumentationBundleTests(unittest.TestCase):
    def test_builds_verified_local_bundle_without_preserving_remote_links(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog = root / "catalog.yaml"
            catalog.write_text(
                """version: v1.36
source_repository: https://github.com/kubernetes/website
documents:
  - id: resource-management
    title: Resource Management
    canonical_url: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
    source_path: content/en/docs/concepts/configuration/manage-resources-containers.md
rules:
  missing-requests:
    document_id: resource-management
    section: Requests and limits
""",
                encoding="utf-8",
            )
            output = root / "bundle"
            build_bundle(
                catalog_path=catalog,
                output_dir=output,
                source_revision="deadbeef",
                fetch=lambda _: "<html><main><h1>Resource Management</h1><p>Requests drive scheduling.</p><a href='https://example.com'>External</a><script>bad()</script></main></html>",
            )

            registry = DocumentRegistry(output)
            document = registry.get("resource-management")

            self.assertIn("Requests drive scheduling.", document.content)
            self.assertNotIn("External", document.content)
            self.assertEqual(document.source_revision, "deadbeef")
            self.assertEqual(registry.source_for_rule("missing-requests")["section"], "Requests and limits")
            self.assertTrue((output / "NOTICE").exists())

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["kubernetes_version"], "v1.36")
            self.assertEqual(manifest["documents"][0]["sha256"], document.sha256)
