"""Loss-conscious text-only projection; never execute or fetch provider markup."""

from html.parser import HTMLParser


class _OcrTextParser(HTMLParser):
    _allowed = {"html", "body", "p", "div", "span", "br", "h1", "h2", "h3",
                "h4", "h5", "h6", "table", "thead", "tbody", "tfoot", "tr",
                "td", "th", "b", "strong", "i", "em", "u", "ul", "ol", "li"}
    _line = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.supported = True
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        # Unknown formatting, attributes or active content remain literal raw text.
        if tag not in self._allowed or attrs:
            self.supported = False
        if tag in self._line:
            self.parts.append("\n")
        if tag != "br":
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.supported = False
        if tag in self._line:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_comment(self, data):
        self.supported = False

    def handle_decl(self, decl):
        self.supported = False

    def handle_pi(self, data):
        self.supported = False

    def unknown_decl(self, data):
        self.supported = False


def ocr_display_text(raw: str) -> tuple[str, bool]:
    """Return display text and conversion flag; the stored source is unchanged.

    Only a complete HTML fence or document wrapper opts into conversion. Ordinary
    text with comparison signs is never parsed as markup. Unsupported/malformed
    HTML stays literal instead of silently dropping potentially material content.
    """
    text = raw.strip()
    if text.startswith("```html\n") and text.endswith("\n```"):
        text = text[len("```html\n"):-len("\n```")].strip()
    elif not (text.lower().startswith("<html>") and text.lower().endswith("</html>")):
        return raw, False
    parser = _OcrTextParser()
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError):
        return raw, False
    if not parser.supported or parser.stack:
        return raw, False
    result = "\n".join(line.strip() for line in "".join(parser.parts).splitlines()
                       if line.strip())
    return (result, True) if result else (raw, False)
