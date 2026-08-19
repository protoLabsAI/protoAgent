"""Tests for fetch_url tool, especially HTML extraction."""

from tools.lg_tools import _extract_text_from_html


class TestExtractTextFromHtml:
    """Test _extract_text_from_html stripping of page chrome."""

    def test_strips_header_nav_footer(self):
        """Should strip header, nav, and footer elements."""
        html = b"""
        <html>
        <head><title>Page</title></head>
        <body>
            <header>Header content</header>
            <nav>Navigation content</nav>
            <main>Main content here</main>
            <footer>Footer content</footer>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Main content here" in result
        assert "Header content" not in result
        assert "Navigation content" not in result
        assert "Footer content" not in result

    def test_strips_aria_roles(self):
        """Should strip elements with ARIA roles: navigation, banner, contentinfo."""
        html = b"""
        <html>
        <body>
            <div role="banner">Banner content</div>
            <div role="navigation">Nav content</div>
            <main>Main content here</main>
            <div role="contentinfo">Content info footer</div>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Main content here" in result
        assert "Banner content" not in result
        assert "Nav content" not in result
        assert "Content info footer" not in result

    def test_prefers_main_tag(self):
        """Should prefer <main> tag when available."""
        html = b"""
        <html>
        <body>
            <header>Header</header>
            <nav>Nav</nav>
            <div>Sidebar content</div>
            <main>Main content here</main>
            <footer>Footer</footer>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Main content here" in result
        assert "Sidebar content" not in result
        assert "Nav" not in result
        assert "Footer" not in result

    def test_fallback_to_article_tag(self):
        """Should fall back to <article> when <main> is absent."""
        html = b"""
        <html>
        <body>
            <header>Header</header>
            <nav>Nav</nav>
            <article>Article content here</article>
            <footer>Footer</footer>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Article content here" in result
        assert "Header" not in result
        assert "Nav" not in result
        assert "Footer" not in result

    def test_fallback_to_role_main(self):
        """Should fall back to role="main" when <main> and <article> are absent."""
        html = b"""
        <html>
        <body>
            <header>Header</header>
            <nav>Nav</nav>
            <div role="main">Role main content here</div>
            <footer>Footer</footer>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Role main content here" in result
        assert "Header" not in result
        assert "Nav" not in result
        assert "Footer" not in result

    def test_fallback_to_body(self):
        """Should fall back to <body> when no main/article/role=main."""
        html = b"""
        <html>
        <body>
            <div>Some content here</div>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Some content here" in result

    def test_complex_page_with_chrome(self):
        """Test a complex page with multiple chrome elements."""
        html = b"""
        <html>
        <head><title>Test Page</title></head>
        <body>
            <header>
                <h1>Site Header</h1>
                <div role="banner">Banner</div>
            </header>
            <nav>
                <ul>
                    <li><a href="/">Home</a></li>
                    <li><a href="/about">About</a></li>
                </ul>
            </nav>
            <div role="navigation">Breadcrumbs</div>
            <main>
                <article>
                    <h1>Article Title</h1>
                    <p>This is the main content that should be extracted.</p>
                    <p>It should not include navigation, headers, or footers.</p>
                </article>
            </main>
            <footer>
                <p>Copyright 2024</p>
                <div role="contentinfo">Footer info</div>
            </footer>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Article Title" in result
        assert "This is the main content that should be extracted." in result
        assert "It should not include navigation, headers, or footers." in result
        assert "Site Header" not in result
        assert "Home" not in result
        assert "Breadcrumbs" not in result
        assert "Copyright 2024" not in result
        assert "Footer info" not in result

    def test_strips_script_and_style(self):
        """Should strip <script> and <style> tags."""
        html = b"""
        <html>
        <head>
            <style>body { color: red; }</style>
            <script>console.log('test');</script>
        </head>
        <body>
            <main>Main content</main>
            <script>alert('should not appear');</script>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Main content" in result
        assert "color: red" not in result
        assert "console.log" not in result
        assert "should not appear" not in result

    def test_whitespace_normalization(self):
        """Should strip leading/trailing whitespace from lines."""
        html = b"""
        <html>
        <body>
            <main>
                <p>Line 1</p>
                <p>Line 2</p>
                <p>    Line 3    </p>
            </main>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        lines = result.split("\n")
        # Should have content lines without extra whitespace
        assert any("Line" in line for line in lines)
        # Should not have lines with only whitespace
        assert all(line.strip() for line in lines)

    def test_empty_html(self):
        """Should handle empty or minimal HTML gracefully."""
        html = b"<html></html>"
        result = _extract_text_from_html(html)
        assert isinstance(result, str)

    def test_html_with_noscript(self):
        """Should strip <noscript> tags."""
        html = b"""
        <html>
        <body>
            <main>
                Main content
                <noscript>Please enable JavaScript</noscript>
            </main>
        </body>
        </html>
        """
        result = _extract_text_from_html(html)
        assert "Main content" in result
        assert "Please enable JavaScript" not in result
