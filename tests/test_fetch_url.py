"""Tests for ``_extract_text_from_html`` in ``tools/lg_tools.py``.

``fetch_url`` runs every HTML response through this extractor before the
text reaches the model's context, so what it strips (and what it keeps)
is a behavioural contract: page chrome — banner headers, navs, footers,
ARIA landmarks, sidebar/menu/breadcrumb widgets — must go, while article
content must survive. That includes article-level ``<header>`` elements
holding a title/byline, even on pages WITHOUT ``<main>``/``<article>``
wrappers (a common real-world pattern), and elements whose classes merely
*contain* a chrome word. The live-network half of ``fetch_url`` is
covered by offline error-path tests in ``test_starter_tools.py``;
everything here is pure HTML-in / text-out.
"""

from __future__ import annotations

import builtins

from tools.lg_tools import _extract_text_from_html


def extract(html: str) -> str:
    return _extract_text_from_html(html.encode("utf-8"))


# ── chrome stripping ─────────────────────────────────────────────────────────


def test_strips_header_nav_footer_and_script_chrome():
    out = extract(
        """
        <html><body>
          <header>Site Header Banner</header>
          <nav>Global Nav Links</nav>
          <div id="content"><p>The actual article text.</p></div>
          <footer>Footer boilerplate</footer>
          <script>var tracking = 1;</script>
          <style>.a { color: red; }</style>
          <noscript>Please enable JavaScript</noscript>
        </body></html>
        """
    )
    assert "The actual article text." in out
    assert "Site Header Banner" not in out
    assert "Global Nav Links" not in out
    assert "Footer boilerplate" not in out
    assert "var tracking" not in out
    assert "color: red" not in out
    assert "enable JavaScript" not in out


def test_strips_aria_role_chrome():
    out = extract(
        """
        <body>
          <div role="banner">Reddit-style top bar</div>
          <div role="navigation">Communities list</div>
          <div class="feed"><p>A long post body that is the real content of the page.</p></div>
          <div role="contentinfo">Terms and privacy links</div>
        </body>
        """
    )
    assert "real content of the page" in out
    assert "top bar" not in out
    assert "Communities list" not in out
    assert "Terms and privacy" not in out


def test_strips_exact_chrome_classes():
    out = extract(
        """
        <body>
          <div class="sidebar">Trending sidebar widget</div>
          <ul class="menu"><li>Nav menu item one</li></ul>
          <ol class="breadcrumb"><li>Home / Section</li></ol>
          <div class="post-body"><p>Substantial article prose lives here.</p></div>
        </body>
        """
    )
    assert "Substantial article prose" in out
    assert "Trending sidebar widget" not in out
    assert "Nav menu item one" not in out
    assert "Home / Section" not in out


def test_keeps_classes_that_merely_contain_chrome_words():
    out = extract(
        """
        <body>
          <div class="story">
            <ul><li class="menu-item">Products page link</li></ul>
            <p class="sidebar-open">Panel intro text</p>
            <p>Long enough story text to dominate the page body easily.</p>
          </div>
        </body>
        """
    )
    assert "Products page link" in out
    assert "Panel intro text" in out
    assert "Long enough story text" in out


def test_nested_chrome_does_not_crash():
    # Chrome nested inside chrome: decomposing the outer element leaves the
    # inner one already-decomposed inside the same find_all pass — the
    # ``decomposed`` guards must skip it rather than blow up.
    out = extract(
        """
        <body>
          <header><header>Inner banner</header></header>
          <div role="navigation"><div role="navigation">Nested nav list</div></div>
          <div class="menu"><div class="menu">Nested menu</div></div>
          <div class="real"><p>Content that survives the sweep.</p></div>
        </body>
        """
    )
    assert "Content that survives the sweep." in out
    assert "Inner banner" not in out
    assert "Nested nav list" not in out
    assert "Nested menu" not in out


# ── <header> is dual-use: banners go, article headers stay ───────────────────


def test_keeps_article_header_inside_main_strips_site_header():
    out = extract(
        """
        <body>
          <header>Site Chrome Banner</header>
          <main>
            <header><h1>Story Title</h1><p>By Jane Doe</p></header>
            <p>Story body.</p>
          </main>
        </body>
        """
    )
    assert "Story Title" in out
    assert "By Jane Doe" in out
    assert "Story body." in out
    assert "Site Chrome Banner" not in out


def test_keeps_article_header_on_page_without_semantic_wrappers():
    # The regression case: no <main>/<article> anywhere, and the article's
    # title/byline live in a <header> nested inside a plain <div>. Only
    # banner-position headers (direct children of <body>) are chrome here —
    # this one must survive, and the dominant-div fallback must not narrow
    # past it either.
    out = extract(
        """
        <body>
          <div class="post">
            <header><h1>Falcons Win The Cup</h1><p>By Jane Doe</p></header>
            <div class="post-content">
              <p>A very long body of article text that runs on and on, easily
              dominating the page so the fallback would otherwise narrow down
              to this container alone.</p>
            </div>
          </div>
        </body>
        """
    )
    assert "Falcons Win The Cup" in out
    assert "By Jane Doe" in out
    assert "dominating the page" in out


def test_strips_nav_wrapping_header_even_when_nested():
    # A header that wraps a <nav> is chrome no matter how deep it sits.
    out = extract(
        """
        <body>
          <div id="shell">
            <header class="top"><span>BrandName</span><nav>Home About Contact</nav></header>
            <div id="post"><p>The story text is here and it is long enough to matter.</p></div>
          </div>
        </body>
        """
    )
    assert "The story text is here" in out
    assert "BrandName" not in out
    assert "Home About Contact" not in out


def test_keeps_section_header():
    out = extract(
        """
        <body>
          <div class="wrap">
            <section>
              <header><h2>Chapter Two</h2></header>
              <p>The section prose continues at length below its heading.</p>
            </section>
          </div>
        </body>
        """
    )
    assert "Chapter Two" in out
    assert "section prose" in out


# ── content-root selection ───────────────────────────────────────────────────


def test_prefers_main_over_rest_of_body():
    out = extract(
        """
        <body>
          <div class="promo">Download our app today</div>
          <main><p>Only this should be extracted.</p></main>
        </body>
        """
    )
    assert "Only this should be extracted." in out
    assert "Download our app" not in out


def test_falls_back_to_role_main_when_no_semantic_tags():
    out = extract(
        """
        <body>
          <div class="promo">Get our app banner</div>
          <div role="main"><p>Signal in the noise.</p></div>
        </body>
        """
    )
    assert "Signal in the noise." in out
    assert "Get our app banner" not in out


def test_falls_back_to_dominant_div_on_div_soup():
    out = extract(
        """
        <body>
          <div id="shell">
            <div id="chatter">Join our community today</div>
            <div id="post">
              <p>Paragraph one of the actual post, which runs long enough to dominate.</p>
              <p>Paragraph two continues the post with even more substantial text.</p>
            </div>
          </div>
        </body>
        """
    )
    assert "Paragraph one of the actual post" in out
    assert "Paragraph two continues the post" in out
    assert "Join our community today" not in out


def test_falls_back_to_body_when_no_div_dominates():
    out = extract(
        """
        <body>
          <div>First half of the page text, about as long as the other one.</div>
          <div>Second half of the page text, about as long as the first one.</div>
        </body>
        """
    )
    assert "First half of the page text" in out
    assert "Second half of the page text" in out


def test_empty_and_trivial_input():
    assert extract("") == ""
    assert "just plain words" in extract("just plain words")


# ── regex fallback (no bs4) is unchanged ─────────────────────────────────────


def test_regex_fallback_without_bs4(monkeypatch):
    real_import = builtins.__import__

    def no_bs4(name, *args, **kwargs):
        if name == "bs4":
            raise ImportError("bs4 unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_bs4)
    out = extract(
        "<html><body><script>var t = 1;</script><nav>Nav text</nav>"
        "<header>Header text</header><p>Hello   world</p></body></html>"
    )
    # Tags stripped, script contents removed, whitespace collapsed…
    assert "Hello world" in out
    assert "var t" not in out
    # …and chrome TEXT is retained — the fallback never stripped nav/header
    # content and still must not (the smart stripping is bs4-only).
    assert "Nav text" in out
    assert "Header text" in out
