from tgexporter.image_search import extract_google_image_urls
from tgexporter import image_search


def test_extract_google_image_urls_reads_imgurl_and_original_urls():
    html = (
        'href="/imgres?imgurl=https%3A%2F%2Fexample.com%2Fopenai-modal.jpg&imgrefurl=https://news.example/a" '
        '["https:\\/\\/cdn.example.com\\/article.webp"] '
        'src="https://encrypted-tbn0.gstatic.com/images?q=tbn:abc123\\u0026usqp=CAU"'
    )

    assert extract_google_image_urls(html) == [
        "https://example.com/openai-modal.jpg",
        "https://cdn.example.com/article.webp",
        "https://encrypted-tbn0.gstatic.com/images?q=tbn:abc123&usqp=CAU",
    ]


def test_find_bing_image_urls_filters_by_required_terms(monkeypatch):
    html = (
        '<a m="{&quot;murl&quot;:&quot;https://cats.example/cat.jpg&quot;,&quot;t&quot;:&quot;Cat art&quot;}"></a>'
        '<a m="{&quot;murl&quot;:&quot;https://news.example/openai-modal.jpg&quot;,&quot;t&quot;:&quot;OpenAI Modal incident&quot;}"></a>'
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit):
            return html.encode("utf-8")

    monkeypatch.setattr(image_search.urllib.request, "urlopen", lambda request, timeout=12: FakeResponse())

    assert image_search.find_bing_image_urls("OpenAI Modal", required_terms=["openai", "modal"]) == [
        "https://news.example/openai-modal.jpg"
    ]
