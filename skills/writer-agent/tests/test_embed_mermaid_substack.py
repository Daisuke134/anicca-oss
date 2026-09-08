import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = (
    Path(__file__).parents[1]
    / "scripts"
    / "_shared"
    / "embed-mermaid-substack.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("embed_mermaid_substack", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ManagedImmutableMediaTest(unittest.TestCase):
    def test_body_image_replaces_mermaid_without_rerendering(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "article.md"
            output = root / "embedded.md"
            body_image = root / "body-diagram.png"
            source.write_text(
                "# Title\n\n```mermaid\ngraph TD; A-->B;\n```\n",
                encoding="utf-8",
            )
            body_image.write_bytes(b"immutable-png")

            with (
                patch.object(
                    module,
                    "render_mermaid_to_png",
                    side_effect=AssertionError("immutable media must not be rerendered"),
                ),
                patch.object(
                    module,
                    "upload",
                    return_value="https://cdn.example/body-diagram.png",
                ),
                patch.object(module, "load_cache", return_value={}),
                patch.dict(
                    os.environ,
                    {
                        "SUBSTACK_SESSION_COOKIE": "test-cookie",
                        "SUBSTACK_PUBLICATION": "writer.example",
                    },
                ),
            ):
                try:
                    result = module.main(
                        [
                            "--markdown-file",
                            str(source),
                            "--out",
                            str(output),
                            "--body-image",
                            str(body_image),
                        ]
                    )
                except SystemExit as error:
                    self.fail(f"managed immutable body media was refused: {error}")

            self.assertEqual(result, 0)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "# Title\n\n![figure](https://cdn.example/body-diagram.png)\n",
            )


if __name__ == "__main__":
    unittest.main()
