import unittest

from job_search_loop.mercor_auth_readback import classify_auth_snapshot


class MercorAuthReadbackTests(unittest.TestCase):
    def test_requires_authenticated_official_surface(self):
        self.assertEqual(classify_auth_snapshot(
            url="https://work.mercor.com/explore",
            visible_text="Explore Applications Earnings Profile",
        ), "authenticated")
        self.assertEqual(classify_auth_snapshot(
            url="https://work.mercor.com/explore",
            visible_text="Explore Profile Sign in",
        ), "logged_out")
        self.assertEqual(classify_auth_snapshot(
            url="https://work.mercor.com/login",
            visible_text="Continue to Mercor Google",
        ), "logged_out")
        self.assertEqual(classify_auth_snapshot(
            url="https://work.mercor.com/jobs/apply/candidate-one?returnPath=%2Fexplore",
            visible_text=(
                "General business strategy Evaluator Application\n"
                "3 of 3 steps done\nWork Authorization\n"
                "Your application has been submitted!\nView application"
            ),
        ), "authenticated")
        self.assertEqual(classify_auth_snapshot(
            url="https://work.mercor.com/jobs/apply/candidate-one?returnPath=%2Fexplore",
            visible_text="Sign in to continue your application",
        ), "logged_out")
        self.assertEqual(classify_auth_snapshot(
            url="https://example.com/",
            visible_text="Explore Applications Earnings Profile",
        ), "indeterminate")


if __name__ == "__main__":
    unittest.main()
