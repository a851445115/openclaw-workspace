import importlib.util
import pathlib
import unittest

SKILL_DIR = pathlib.Path(__file__).resolve().parent.parent
SCRAPE_PATH = SKILL_DIR / 'scrape.py'

spec = importlib.util.spec_from_file_location('scrape_mod', SCRAPE_PATH)
scrape_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scrape_mod)


class FakeElement:
    def __init__(self, text):
        self.text = text
        self.attrib = {'href': text}
        self.html = f'<x>{text}</x>'
        self.markdown = text


class FakePage:
    def __init__(self):
        self.css_calls = []
        self.xpath_calls = []

    def css(self, selector, **kwargs):
        self.css_calls.append((selector, kwargs))
        return [FakeElement(f'css:{selector}')]

    def xpath(self, selector):
        self.xpath_calls.append(selector)
        return [FakeElement(f'xpath:{selector}')]


class SelectorEngineTests(unittest.TestCase):
    def setUp(self):
        self.page = FakePage()
        scrape_mod.Fetcher.get = lambda *args, **kwargs: self.page

    def test_plain_tag_selector_uses_css_not_xpath(self):
        result = scrape_mod.scrape(
            url='https://example.com',
            selector='title',
            mode='basic',
            extract='text',
        )
        self.assertEqual(result, ['css:title'])
        self.assertEqual(self.page.xpath_calls, [])

    def test_explicit_xpath_selector_uses_xpath(self):
        result = scrape_mod.scrape(
            url='https://example.com',
            selector='//title',
            mode='basic',
            extract='text',
        )
        self.assertEqual(result, ['xpath://title'])
        self.assertEqual(self.page.css_calls, [])


if __name__ == '__main__':
    unittest.main()
