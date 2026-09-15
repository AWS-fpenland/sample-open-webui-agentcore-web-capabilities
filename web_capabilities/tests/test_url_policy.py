import pytest
import json
from pathlib import Path

from web_capabilities.documents import DocumentParser
from web_capabilities.url_policy import URLPolicy, URLPolicyError


@pytest.fixture
def policy():
    return URLPolicy({"example.com": {"exact": ["/"], "prefix": ["/docs/"]}})


@pytest.mark.parametrize("url", ["https://example.com", "https://example.com:443/#section", "https://example.com/"])
def test_normalized_endpoint(policy, url):
    assert policy.validate(url) == "https://example.com/"


def test_scoped_directory(policy):
    assert policy.validate("https://example.com/docs/read.html") == "https://example.com/docs/read.html"


@pytest.mark.parametrize("url", ["http://example.com/", "https://example.com/delete", "https://example.com/?delete=1",
    "https://example.com/?", "https://user@example.com/", "https://example.com:8443/", "https://example.com./",
    "https://EXAMPLE.com/", "https://example.com/docs/../private", "https://example.com/docs/%2e%2e/private",
    "https://example.com/docs//private", "https://example.com/docs/\\private", "https://example.com.evil.invalid/",
    "https://127.0.0.1/", "https://[::1]/", "https://169.254.169.254/", "https://example.com/\n",
    "https://example.com/docs/;delete", "file:///tmp/secret"])
def test_shared_policy_rejects_before_network(policy, url):
    with pytest.raises(URLPolicyError):
        policy.validate(url)


@pytest.mark.parametrize("rules", [{}, {"example.com": {"prefix": ["/"]}}, {"example.com": {"exact": []}},
    {"example.com": {"prefix": ["/docs"]}}, {"example.com": {"exact": ["/../"]}}])
def test_configuration_fails_closed(rules):
    with pytest.raises(URLPolicyError):
        URLPolicy(rules)


def test_static_extraction_bounds_and_untrusted_links():
    parser = DocumentParser("https://example.com/docs/", max_text=8)
    parser.feed('<title>Sample</title><script>private()</script><p>Visible text</p><a href="next">Next</a>')
    result = parser.document("https://example.com/docs/#fragment")
    assert result["text"] == "Visible "
    assert result["title"] == "Sample"
    assert result["truncated"] is True
    assert result["links"] == ["https://example.com/docs/next"]
    assert result["source_capability"] == "bounded-https"


def test_production_javascript_fixture_dependencies_are_explicitly_allowed():
    rules = json.loads((Path(__file__).resolve().parents[2] / "config/web-canary-url-policy.json").read_text())
    policy = URLPolicy(rules)
    for path in ("/js/", "/static/jquery.js", "/static/bootstrap.min.css", "/static/main.css"):
        assert policy.validate("https://quotes.toscrape.com" + path) == "https://quotes.toscrape.com" + path
    with pytest.raises(URLPolicyError):
        policy.validate("https://quotes.toscrape.com/static/other.js")
