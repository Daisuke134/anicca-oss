import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "substack-publish"
    / "verify-preview.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("verify_preview", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with mock.patch.dict(
        "os.environ", {"SUBSTACK_PUBLICATION": "writer-test.substack.com"}
    ):
        spec.loader.exec_module(module)
    return module


class VerifyPreviewRetryTests(unittest.TestCase):
    def test_target_closed_is_retried_with_a_fresh_page(self):
        module = load_module()
        closed = module.TargetClosedError("page closed")
        with (
            mock.patch.object(
                module,
                "verify_once",
                side_effect=[closed, closed, 0],
            ) as verify_once,
            mock.patch.object(module.time, "sleep"),
        ):
            result = module.main(["208760780", "950"])

        self.assertEqual(result, 0)
        self.assertEqual(verify_once.call_count, 3)

    def test_real_preview_failure_is_not_retried(self):
        module = load_module()
        with mock.patch.object(
            module,
            "verify_once",
            return_value=1,
        ) as verify_once:
            result = module.main(["208760780", "950"])

        self.assertEqual(result, 1)
        verify_once.assert_called_once()


if __name__ == "__main__":
    unittest.main()
