"""Tests for `PaginationDetector` and `CrawlSpider.follow_pagination`."""

import pytest

from scrapling.engines.toolbelt.custom import Response
from scrapling.spiders import PaginationDetector
from scrapling.spiders.request import Request
from scrapling.spiders.templates import CrawlSpider
from scrapling.core._types import Any, AsyncGenerator, Dict, Union


def _make_response(html: str, url: str = "https://example.com/blog/") -> Response:
    resp = Response(
        url=url,
        content=html,
        status=200,
        reason="OK",
        cookies={},
        headers={},
        request_headers={},
    )
    resp.request = Request(url)
    return resp


def _next(html: str, url: str = "https://example.com/blog/", **kwargs: Any):
    return PaginationDetector(**kwargs).next_page(_make_response(html, url))


class TestNextPage:
    def test_rel_next_link_in_head(self):
        html = '<html><head><link rel="next" href="/blog/page/2/"></head><body><a href="/x">x</a></body></html>'
        assert _next(html) == "https://example.com/blog/page/2/"

    def test_rel_next_on_anchor_with_other_tokens(self):
        html = '<a href="/a">a</a><a rel="nofollow NEXT" href="?page=2">go</a>'
        assert _next(html) == "https://example.com/blog/?page=2"

    @pytest.mark.parametrize("label", ["Next", "next page", "Next »", "‹ Older posts", "Suivant", "›", "→"])
    def test_next_text_labels(self, label: str):
        html = f'<a href="/blog/post-1">Post</a><a href="/blog/page/2/">{label}</a>'
        assert _next(html) == "https://example.com/blog/page/2/"

    def test_aria_label_and_title(self):
        assert _next('<a href="/p2" aria-label="Next page"><svg></svg></a>') == "https://example.com/p2"
        assert _next('<a href="/p3" title="Next"><i class="icon"></i></a>') == "https://example.com/p3"

    def test_label_must_match_whole_text(self):
        html = '<a href="/next-article">Read next article</a><a href="/more">Read more</a>'
        assert _next(html) is None

    def test_double_arrow_is_not_next(self):
        assert _next('<a href="/blog/page/9/">»»</a>') is None

    def test_next_class_on_anchor_or_parent(self):
        assert _next('<a class="pagination-next" href="/p/2">2</a>') == "https://example.com/p/2"
        assert _next('<ul><li class="next"><a href="/p/2">→ go</a></li></ul>') == "https://example.com/p/2"

    def test_next_token_is_word_bounded(self):
        assert _next('<a class="nextjs-link" href="/docs">docs</a>') is None

    def test_disabled_next_is_ignored(self):
        html = (
            '<a href="/p/2" aria-disabled="true">Next</a>'
            '<li class="disabled"><a href="/p/3">Next</a></li>'
            '<a class="next disabled" href="/p/4">x</a>'
        )
        assert _next(html) is None

    def test_numbered_pagination_uses_marked_current_page(self):
        html = """
        <nav class="pagination">
          <a href="/list?page=1">1</a>
          <a href="/list?page=2" aria-current="page">2</a>
          <a href="/list?page=3">3</a>
        </nav>
        """
        assert _next(html, url="https://example.com/list") == "https://example.com/list?page=3"

    def test_numbered_pagination_uses_url_page_number(self):
        html = '<div class="pager"><a href="/list/page/3">3</a><a href="/list/page/5">5</a><a href="/list/page/4">4</a></div>'
        assert _next(html, url="https://example.com/list/page/3") == "https://example.com/list/page/4"

    def test_numbered_pagination_assumes_first_page(self):
        html = '<ul class="page-numbers"><li><span>1</span></li><li><a href="/shop/page/2/">2</a></li></ul>'
        assert _next(html, url="https://example.com/shop/") == "https://example.com/shop/page/2/"

    def test_numbers_outside_pagination_container_are_ignored(self):
        assert _next('<div class="comments"><a href="/c/2">2</a></div>') is None

    def test_incremented_url_fallback(self):
        html = '<a href="/items?sort=new&page=5">jump</a><a href="/items?sort=new&page=8">later</a>'
        assert (
            _next(html, url="https://example.com/items?sort=new&page=7") == "https://example.com/items?page=8&sort=new"
        )

    def test_skips_current_page_fragments_and_javascript(self):
        html = '<a href="#">Next</a><a href="javascript:void(0)">Next</a><a href="/blog/">Next</a>'
        assert _next(html) is None

    def test_same_domain_by_default(self):
        html = '<a rel="next" href="https://other.com/page/2">Next</a>'
        assert _next(html) is None
        assert _next(html, same_domain=False) == "https://other.com/page/2"

    def test_custom_next_texts(self):
        html = '<a href="/p2">Mehr laden</a>'
        assert _next(html) is None
        assert _next(html, next_texts=["mehr laden"]) == "https://example.com/p2"

    def test_rel_next_has_priority_over_text(self):
        html = '<a href="/by-text">Next</a><link rel="next" href="/by-rel">'
        assert _next(html) == "https://example.com/by-rel"

    def test_no_pagination(self):
        assert _next("<html><body><p>Single page</p></body></html>") is None


class TestPageUrls:
    def test_collects_next_prev_and_numbered_pages(self):
        html = """
        <link rel="prev" href="/blog/page/1/">
        <nav class="navigation pagination">
          <a class="prev" href="/blog/page/1/">Previous</a>
          <a href="/blog/page/1/">1</a>
          <span aria-current="page">2</span>
          <a href="/blog/page/3/">3</a>
          <a href="/blog/page/2/">2</a>
          <a class="next" href="/blog/page/3/">Next</a>
        </nav>
        <a href="/about">About</a>
        """
        urls = PaginationDetector().page_urls(_make_response(html, "https://example.com/blog/page/2/"))
        assert urls == ["https://example.com/blog/page/3/", "https://example.com/blog/page/1/"]

    def test_empty_when_no_pagination(self):
        assert PaginationDetector().page_urls(_make_response('<a href="/about">About</a>')) == []


class _PaginatedSpider(CrawlSpider):
    name = "paginated"
    start_urls = ["https://example.com/blog/"]
    follow_pagination = True

    async def parse_listing(self, response: Response) -> AsyncGenerator[Union[Dict[str, Any], Request, None], None]:
        yield {"url": response.url}


async def _collect(agen: AsyncGenerator) -> list:
    return [item async for item in agen]


class TestCrawlSpiderPagination:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        class S(CrawlSpider):
            name = "s"
            start_urls = ["https://example.com/blog/"]

        assert S.follow_pagination is False
        results = await _collect(S().parse(_make_response('<a rel="next" href="/blog/page/2/">Next</a>')))
        assert results == []

    @pytest.mark.asyncio
    async def test_follows_next_page_with_same_callback(self):
        spider = _PaginatedSpider()
        response = _make_response('<a rel="next" href="/blog/page/2/">Next</a>')
        response.request = Request("https://example.com/blog/", callback=spider.parse_listing)
        results = await _collect(spider.parse(response))

        assert len(results) == 1
        assert isinstance(results[0], Request)
        assert results[0].url == "https://example.com/blog/page/2/"
        assert results[0].callback == spider.parse_listing

    @pytest.mark.asyncio
    async def test_no_request_on_last_page(self):
        results = await _collect(_PaginatedSpider().parse(_make_response("<p>last page</p>")))
        assert results == []

    @pytest.mark.asyncio
    async def test_custom_detector(self):
        class S(_PaginatedSpider):
            def pagination_detector(self) -> PaginationDetector:
                return PaginationDetector(next_texts=["mehr"])

        results = await _collect(S().parse(_make_response('<a href="/p2">Mehr</a>')))
        assert [r.url for r in results] == ["https://example.com/p2"]
