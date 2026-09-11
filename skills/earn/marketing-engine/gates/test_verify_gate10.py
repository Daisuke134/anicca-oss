import unittest
from pathlib import Path

from verify_gate10 import verify_gate10


ENGINE = Path(__file__).resolve().parent.parent


class Gate10VerifierTest(unittest.TestCase):
    def test_live_gate10_evidence_passes(self):
        result = verify_gate10(ENGINE)
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"], {
            "products": 4, "accounts": 9, "renderers": 5, "safe_plans": 2,
        })
        self.assertEqual(result["plans_by_product"], {"ebook-en": 1, "ebook-ja": 1})
        self.assertEqual(result["legacy_production_references"], 0)
        self.assertEqual(result["enabled_legacy_publishers"], 0)
        self.assertEqual(result["app_candidate_counts"], {"anicca-ios": 0, "honne-ai": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
