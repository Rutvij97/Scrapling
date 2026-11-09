"""Heuristic detection of pagination URLs in a page."""

import re
from urllib.parse import parse_qsl, urlsplit

from w3lib.html import strip_html5_whitespace
from w3lib.url import canonicalize_url, safe_url_string

from scrapling.core._types import (
    TYPE_CHECKING,
    Any,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
)
from scrapling.core.utils import log

if TYPE_CHECKING:
    from scrapling.parser import Selector


__all__ = ["PaginationDetector", "DEFAULT_NEXT_TEXTS", "DEFAULT_PAGE_PARAMS"]


DEFAULT_NEXT_TEXTS = (
    "next",
    "next page",
    "next posts",
    "next results",
    "older",
    "older posts",
    "older entries",
    "suivant",
    "page suivante",
    "siguiente",
    "weiter",
    "nächste",
    "nächste seite",
    "próxima",
    "próximo",
    "successivo",
    "avanti",
    "volgende",
    "следующая",
    "далее",
    "次へ",
    "下一页",
    "다음",
)
DEFAULT_PAGE_PARAMS = ("page", "p", "pg", "paged", "pagenum", "page_num", "pagenumber", "page_number")

# Arrow-only labels that commonly mean "next". Double arrows (`»»`, `>>`) usually mean "last", so they're excluded.
_NEXT_ARROWS = {"»", "›", "→", ">", "⟩", "❯"}
_ARROW_CHARS = "»›→>⟩❯«‹←<⟨❮ \t\n\r "
_NEXT_TOKEN = re.compile(r"(?:^|[-_])next(?:$|[-_])", re.IGNORECASE)
_DISABLED_TOKENS = {"disabled", "is-disabled", "inactive"}
_CURRENT_TOKENS = {"active", "current", "selected", "is-active", "is-current"}
_PAGINATION_HINT = re.compile(r"pag(?:ination|inator|er|ing|es)|page-numbers|page-nav", re.IGNORECASE)
_PATH_PAGE = re.compile(r"/(?:page|p|pg)/(\d+)(?=/|$)", re.IGNORECASE)
_PAGINATION_CONTAINERS = (
    "//*[@role='navigation' or self::nav or self::ul or self::ol or self::div or self::section or self::p]"
    "[re:test(concat(@class, ' ', @id, ' ', @aria-label), '{pattern}', 'i')]"
)


def _tokens(value: Any) -> Set[str]:
    return set(str(value or "").lower().split())


def _normalize_label(value: Any) -> str:
    return " ".join(str(value or "").split()).lower()


class PaginationDetector:
    """Find pagination links in a page without writing site-specific selectors.

    The detector never makes requests or follows anything on its own; it only reads the page you give it, so
    it's safe to use from any `Spider` callback. Signals are checked from the most to the least reliable:

    1. A `rel="next"` link (`<link>` in the head, or `<a>`/`<area>` in the body).
    2. A link whose text, `aria-label`, or `title` is a "next" label (see `next_texts`) or a single arrow (`›`, `»`, `→`).
    3. A link whose own or parent `class`/`id` contains a `next` token (`next`, `pagination-next`, `nav-next`, ...).
    4. Inside a pagination container, the link numbered one after the current page.
    5. A link to the current URL with its page number (query parameter or `/page/N` path segment) incremented.

    Links that are disabled, point to the current page, use a non-HTTP scheme, or leave the current domain (unless
    `same_domain=False`) are ignored.

    :param next_texts: Lower-case labels that mean "next page". Compared to the whole normalized link label,
        ignoring surrounding arrows, so `Next »` and `‹ Older posts` match but `Read next article` doesn't.
    :param page_params: Query parameter names that hold the page number, used to find the current page number.
    :param same_domain: Only return links on the same host as the page. Default True.
    :param canonicalize: Canonicalize returned URLs like `LinkExtractor` does. Default True.
    """

    def __init__(
        self,
        next_texts: Iterable[str] = DEFAULT_NEXT_TEXTS,
        page_params: Iterable[str] = DEFAULT_PAGE_PARAMS,
        same_domain: bool = True,
        canonicalize: bool = True,
    ) -> None:
        self.next_texts: Set[str] = {_normalize_label(text) for text in next_texts}
        self.page_params: Tuple[str, ...] = tuple(param.lower() for param in page_params)
        self.same_domain = same_domain
        self.canonicalize = canonicalize

    def next_page(self, page: "Selector") -> Optional[str]:
        """Return the absolute URL of the next page, or None if no pagination link was found."""
        root = page._root
        current_url = self._clean(page, page.url)

        for href in root.xpath(
            "//*[self::link or self::a or self::area][@href]"
            "[contains(concat(' ', normalize-space(translate(@rel, 'NEXT', 'next')), ' '), ' next ')]/@href"
        ):
            url = self._resolve(page, str(href), current_url)
            if url:
                return url

        anchors = [anchor for anchor in root.xpath("//a[@href] | //area[@href]") if not self._is_disabled(anchor)]
        for anchor in anchors:
            if self._has_next_label(anchor):
                url = self._resolve(page, anchor.get("href"), current_url)
                if url:
                    return url

        for anchor in anchors:
            parent = anchor.getparent()
            names = [anchor.get("class"), anchor.get("id")]
            if parent is not None:
                names += [parent.get("class"), parent.get("id")]
            if any(_NEXT_TOKEN.search(token) for name in names for token in _tokens(name)):
                url = self._resolve(page, anchor.get("href"), current_url)
                if url:
                    return url

        current_number = self._current_page_number(page)
        for container in self._containers(root):
            number = current_number or self._marked_page_number(container) or 1
            for anchor in container.xpath(".//a[@href]"):
                if not self._is_disabled(anchor) and _normalize_label(anchor.text_content()) == str(number + 1):
                    url = self._resolve(page, anchor.get("href"), current_url)
                    if url:
                        return url

        if current_number is not None:
            wanted = {self._clean(page, candidate) for candidate in self._incremented_urls(page.url, current_number)}
            for anchor in anchors:
                url = self._resolve(page, anchor.get("href"), current_url)
                if url and url in wanted:
                    return url

        return None

    def page_urls(self, page: "Selector") -> List[str]:
        """Return every pagination URL found on the page (the next page first, then numbered pages), deduplicated."""
        current_url = self._clean(page, page.url)
        found: List[str] = []
        next_url = self.next_page(page)
        if next_url:
            found.append(next_url)

        for href in page._root.xpath(
            "//*[self::link or self::a][@href]"
            "[contains(concat(' ', normalize-space(translate(@rel, 'PREV', 'prev')), ' '), ' prev ')]/@href"
        ):
            url = self._resolve(page, str(href), current_url)
            if url:
                found.append(url)

        for container in self._containers(page._root):
            for anchor in container.xpath(".//a[@href]"):
                if self._is_disabled(anchor):
                    continue
                url = self._resolve(page, anchor.get("href"), current_url)
                if url:
                    found.append(url)

        return list(dict.fromkeys(found))

    def _containers(self, root: Any) -> List[Any]:
        containers = root.xpath(
            _PAGINATION_CONTAINERS.format(pattern=_PAGINATION_HINT.pattern),
            namespaces={"re": "http://exslt.org/regular-expressions"},
        )
        # Keep the innermost matches only, so a `div.pagination > ul.pagination` pair isn't scanned twice.
        container_set = set(containers)
        return [
            element
            for element in containers
            if not any(descendant in container_set for descendant in element.iterdescendants())
        ]

    def _has_next_label(self, anchor: Any) -> bool:
        for raw in (anchor.text_content(), anchor.get("aria-label"), anchor.get("title")):
            label = _normalize_label(raw)
            if not label:
                continue
            if label in _NEXT_ARROWS:
                return True
            if label.strip(_ARROW_CHARS) in self.next_texts:
                return True
        return False

    @staticmethod
    def _is_disabled(element: Any) -> bool:
        if element.get("aria-disabled", "").lower() == "true" or element.get("disabled") is not None:
            return True
        parent = element.getparent()
        for node in (element, parent):
            if node is not None and _tokens(node.get("class")) & _DISABLED_TOKENS:
                return True
        return False

    @staticmethod
    def _marked_page_number(container: Any) -> Optional[int]:
        for element in container.iter():
            if not isinstance(element.tag, str):
                continue
            if element.get("aria-current", "").lower() == "page" or _tokens(element.get("class")) & _CURRENT_TOKENS:
                label = _normalize_label(element.text_content())
                if label.isdigit():
                    return int(label)
        return None

    def _current_page_number(self, page: "Selector") -> Optional[int]:
        parts = urlsplit(page.url)
        for key, value in parse_qsl(parts.query):
            if key.lower() in self.page_params and value.isdigit():
                return int(value)
        match = _PATH_PAGE.search(parts.path)
        return int(match.group(1)) if match else None

    def _incremented_urls(self, url: str, current: int) -> List[str]:
        parts = urlsplit(url)
        candidates: List[str] = []
        for param in self.page_params:
            pattern = re.compile(rf"(?:(?<=^)|(?<=[&;])){re.escape(param)}={current}(?=$|[&;])", re.IGNORECASE)
            if pattern.search(parts.query):
                query = pattern.sub(f"{param}={current + 1}", parts.query, count=1)
                candidates.append(parts._replace(query=query).geturl())
        path = _PATH_PAGE.sub(lambda m: m.group(0).replace(f"/{current}", f"/{current + 1}"), parts.path, count=1)
        if path != parts.path:
            candidates.append(parts._replace(path=path).geturl())
        return candidates

    def _clean(self, page: "Selector", url: str) -> str:
        if self.canonicalize:
            url = canonicalize_url(url)
        return safe_url_string(url, encoding=getattr(page, "encoding", None) or "utf-8")

    def _resolve(self, page: "Selector", href: Optional[str], current_url: str) -> Optional[str]:
        href = strip_html5_whitespace(href or "")
        if not href or href.startswith("#"):
            return None
        try:
            url = self._clean(page, page.urljoin(href))
        except ValueError:
            log.debug(f"Skipping the bad pagination URL {href!r}")
            return None

        if url.split("://", 1)[0] not in ("http", "https", "file") or url == current_url:
            return None
        if self.same_domain and urlsplit(url).hostname != urlsplit(current_url).hostname:
            return None
        return url
