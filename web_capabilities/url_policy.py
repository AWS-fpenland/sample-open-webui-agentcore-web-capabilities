"""Curated unauthenticated reading endpoints, shared by HTTP and Browser paths."""

import re
from urllib.parse import urlsplit


class URLPolicyError(ValueError):
    pass


class URLPolicy:
    def __init__(self, rules):
        if not isinstance(rules, dict) or not 1 <= len(rules) <= 10:
            raise URLPolicyError("Configure one to ten curated public hosts")
        self.rules = {}
        for host, paths in rules.items():
            if (not isinstance(host, str) or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", host)
                    or not isinstance(paths, dict) or set(paths) - {"exact", "prefix"}):
                raise URLPolicyError("Invalid curated reading policy")
            validated = {}
            for kind in ("exact", "prefix"):
                entries = paths.get(kind, [])
                if not isinstance(entries, list) or len(entries) > 20:
                    raise URLPolicyError("Invalid curated reading paths")
                for path in entries:
                    if (not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]*", path)
                            or ".." in path or "//" in path or len(path) > 512
                            or (kind == "prefix" and (path == "/" or not path.endswith("/")))):
                        raise URLPolicyError("Invalid curated reading path")
                validated[kind] = tuple(entries)
            if not any(validated.values()):
                raise URLPolicyError("Curated reading paths cannot be empty")
            self.rules[host] = validated

    @property
    def hosts(self):
        return tuple(self.rules)

    def validate(self, url):
        if (not isinstance(url, str) or not 1 <= len(url) <= 2048 or "\\" in url
                or any(ord(character) < 33 or ord(character) > 126 for character in url)):
            raise URLPolicyError("URL outside curated reading policy")
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or parsed.hostname not in self.rules
                    or parsed.query or "?" in url.split("#", 1)[0]
                    or parsed.netloc not in {parsed.hostname, parsed.hostname + ":443"}):
                raise ValueError()
            path = parsed.path or "/"
            if not re.fullmatch(r"/[A-Za-z0-9_./-]*", path) or ".." in path or "//" in path:
                raise ValueError()
            rules = self.rules[parsed.hostname]
            if path not in rules["exact"] and not any(path.startswith(prefix) for prefix in rules["prefix"]):
                raise ValueError()
            return f"https://{parsed.hostname}{path}"
        except (TypeError, ValueError):
            raise URLPolicyError("URL outside curated reading policy") from None
