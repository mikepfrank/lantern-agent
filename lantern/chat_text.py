"""Formatting at the boundary between model output and operator chat."""


def trim_reply_segment(text: str) -> str:
    """Trim a message's padding while retaining Markdown code indentation."""
    body = text.strip()
    if not body:
        return ""
    padding = text[:len(text) - len(text.lstrip())]
    indent = padding.rsplit("\n", 1)[-1].rsplit("\r", 1)[-1]
    if not indent.strip(" \t") and indent.expandtabs(4).startswith("    "):
        return indent + body
    return body
