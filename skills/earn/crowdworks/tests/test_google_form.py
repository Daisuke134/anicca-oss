from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[4]
PATH = ROOT / "skills/earn/crowdworks/scripts/google_form.py"


def load():
    spec = importlib.util.spec_from_file_location("crowdworks_google_form_test", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_short_form_route_gets_bounded_wait_for_exact_official_viewform(tmp_path):
    module = load()
    events = []

    class Locator:
        def get_attribute(self, name):
            return "https://docs.google.com/forms/d/e/form-id/formResponse"
        def count(self): return 0

    class Page:
        url = "https://forms.gle/short"
        def goto(self, *args, **kwargs): pass
        def wait_for_timeout(self, value): pass
        def wait_for_url(self, pattern, timeout):
            events.append(timeout)
            self.url = "https://docs.google.com/forms/d/e/form-id/viewform"
        def locator(self, selector): return Locator()
        def close(self): pass

    class Context:
        def __init__(self): self.page = Page()
        def new_page(self): return self.page

    class Browser:
        contexts = [Context()]

    with pytest.raises(RuntimeError, match="answer-stop"):
        module.submit_once(browser=Browser(), state_root=tmp_path,
                           url="https://forms.gle/short",
                           url_sha256=__import__("hashlib").sha256(b"https://forms.gle/short").hexdigest(),
                           answer_fields=lambda page: (_ for _ in ()).throw(RuntimeError("answer-stop")))
    assert events == [10_000]


def test_non_google_redirect_fails_closed_with_sanitized_host(tmp_path):
    module = load()

    class Page:
        url = "https://accounts.google.com/login"
        def goto(self, *args, **kwargs): pass
        def wait_for_timeout(self, value): pass
        def close(self): pass

    class Context:
        def new_page(self): return Page()

    class Browser:
        contexts = [Context()]

    with pytest.raises(RuntimeError, match=r"google_form_route_invalid:accounts\.google\.com"):
        module.submit_once(browser=Browser(), state_root=tmp_path,
                           url="https://forms.gle/short",
                           url_sha256=__import__("hashlib").sha256(b"https://forms.gle/short").hexdigest(),
                           answer_fields=lambda page: [])
