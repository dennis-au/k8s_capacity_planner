from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kcp.docs import DocumentRegistry, render_document
from kcp.docs_sync import build_bundle


class DocumentationBundleTests(unittest.TestCase):
    def test_renders_local_document_as_escaped_article_sections(self) -> None:
        article = render_document(
            """Limit Ranges
Introduction text for a local document.
Constraints on resource limits and requests
Enforce minimum CPU requests.
Set default memory requests.
apiVersion : v1 kind : LimitRange
<script>alert('not executable')</script>
""",
            "Limit Ranges",
        )

        self.assertEqual([heading.title for heading in article.headings], ["Constraints on resource limits and requests"])
        self.assertIn('<h3 id="constraints-on-resource-limits-and-requests">', article.content)
        self.assertIn("<ul><li>Enforce minimum CPU requests.</li><li>Set default memory requests.</li></ul>", article.content)
        self.assertIn("<pre><code>apiVersion : v1 kind : LimitRange</code></pre>", article.content)
        self.assertIn("&lt;script&gt;alert(&#x27;not executable&#x27;)&lt;/script&gt;", article.content)

    def test_renders_mermaid_flowchart_as_local_relationship_diagram(self) -> None:
        article = render_document(
            """Horizontal Pod Autoscaling
How does a HorizontalPodAutoscaler work?
graph BT hpa[HorizontalPodAutoscaler] --> scale[Scale] subgraph rc[Deployment] scale end scale -.-> pod1[Pod 1] scale -.-> pod2[Pod 2] classDef hpa fill:#D5A6BD class hpa hpa
Figure 1. HPA scale relationships
""",
            "Horizontal Pod Autoscaling",
        )

        self.assertIn('class="doc-diagram"', article.content)
        self.assertIn("HorizontalPodAutoscaler controls Scale.", article.content)
        self.assertIn("Scale updates Pod 1.", article.content)
        self.assertIn("Deployment", article.content)
        self.assertNotIn("graph BT", article.content)
        self.assertNotIn("classDef", article.content)

    def test_all_bundled_flowcharts_render_without_mermaid_source(self) -> None:
        registry = DocumentRegistry(Path("kcp/assets/k8s-docs"))
        flowchart_documents = [
            document
            for document in registry.list()
            if any(line.startswith(("graph ", "flowchart ")) for line in document.content.splitlines())
        ]

        self.assertGreaterEqual(len(flowchart_documents), 3)
        for document in flowchart_documents:
            article = render_document(document.content, document.title)
            self.assertIn('class="doc-diagram"', article.content)
            self.assertNotIn("graph ", article.content)
            self.assertNotIn("flowchart ", article.content)

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
