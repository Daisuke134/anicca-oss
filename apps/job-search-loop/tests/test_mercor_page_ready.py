import unittest

from job_search_loop.mercor_page_ready import approved_page_url


class MercorPageReadyTests(unittest.TestCase):
    def test_accepts_only_blank_or_official_mercor_page(self):
        self.assertTrue(approved_page_url("about:blank", allow_blank=True))
        self.assertTrue(approved_page_url("https://work.mercor.com/explore"))
        self.assertTrue(approved_page_url("https://work.mercor.com/explore?listingId=list_1"))
        self.assertFalse(approved_page_url("about:blank"))
        self.assertFalse(approved_page_url("https://example.com/explore"))
        self.assertFalse(approved_page_url("https://user:pass@work.mercor.com/explore"))


if __name__ == "__main__":
    unittest.main()
