"""A strict parser for the YAML subset used by DUREH packages and org definitions.

Supported: block mappings, block sequences (of scalars or mappings), flow
sequences of scalars (``[a, b]``), flow mappings of scalars (``{a: 1, b: x}``),
plain/quoted scalars, ``#`` comments, and ``|`` literal blocks. Anything else fails loudly instead of being guessed.
"""
from __future__ import annotations

import re

from .util import fail

_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?\d+\.\d+$")


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            break
        out.append(ch)
    return "".join(out).rstrip()


def scalar(text: str):
    t = text.strip()
    if t == "" or t in ("~", "null"):
        return None
    if t in ("true", "false"):
        return t == "true"
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    if t.startswith("[") :
        if not t.endswith("]"):
            fail(f"unterminated flow sequence: {t!r}")
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [scalar(part) for part in _split_flow(inner)]
    if t.startswith("{"):
        if not t.endswith("}"):
            fail(f"unterminated flow mapping: {t!r}")
        inner = t[1:-1].strip()
        out: dict = {}
        for part in _split_flow(inner) if inner else []:
            key, sep, value = part.strip().partition(":")
            key = key.strip().strip("\"'")
            if not sep or not key or (value and not value.startswith(" ")):
                fail(f"expected 'key: value' in flow mapping: {t!r}")
            if key in out:
                fail(f"duplicate key {key!r} in flow mapping: {t!r}")
            out[key] = scalar(value)
        return out
    if _INT.match(t):
        return int(t)
    if _FLOAT.match(t):
        return float(t)
    return t


def _split_flow(inner: str) -> list[str]:
    parts, buf, quote = [], [], None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        elif ch in "[]{}":
            fail(f"nested flow collections are not supported: {inner!r}")
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def loads(text: str, source: str = "<yaml>"):
    raw_lines = text.splitlines()
    lines: list[tuple[int, str, int]] = []  # (indent, content, lineno)
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            fail(f"{source}:{i + 1}: tab indentation is not allowed")
        stripped = _strip_comment(raw)
        if stripped.strip() in ("", "---"):
            i += 1
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        if content.endswith(": |") or content == "- |" or content.endswith(":|"):
            # literal block: gather following more-indented lines verbatim
            block = []
            j = i + 1
            while j < len(raw_lines):
                nxt = raw_lines[j]
                if nxt.strip() and (len(nxt) - len(nxt.lstrip(" "))) <= indent:
                    break
                block.append(nxt)
                j += 1
            nonblank = [b for b in block if b.strip()]
            cut = min((len(b) - len(b.lstrip(" ")) for b in nonblank), default=0)
            literal = "\n".join(b[cut:] for b in block).rstrip("\n") + "\n"
            key = content[:-1].rstrip().rstrip(":").rstrip() if not content.startswith("- ") else None
            lines.append((indent, ("LITERAL", key, literal), i + 1))
            i = j
            continue
        lines.append((indent, content, i + 1))
        i += 1
    value, pos = _parse_block(lines, 0, lines[0][0] if lines else 0, source)
    if pos != len(lines):
        fail(f"{source}:{lines[pos][2]}: unexpected indentation")
    return value


def _parse_block(lines, pos, indent, source):
    if pos >= len(lines):
        return None, pos
    first = lines[pos][1]
    is_seq = isinstance(first, str) and (first == "-" or first.startswith("- "))
    if is_seq:
        return _parse_seq(lines, pos, indent, source)
    return _parse_map(lines, pos, indent, source)


def _parse_map(lines, pos, indent, source):
    result: dict = {}
    while pos < len(lines):
        ind, content, lineno = lines[pos]
        if ind < indent:
            break
        if ind > indent:
            fail(f"{source}:{lineno}: unexpected indentation")
        if isinstance(content, tuple):
            _, key, literal = content
            result[key] = literal
            pos += 1
            continue
        if content.startswith("- "):
            fail(f"{source}:{lineno}: sequence item where a mapping key was expected")
        key, sep, rest = content.partition(":")
        if not sep or (rest and not rest.startswith(" ")):
            fail(f"{source}:{lineno}: expected 'key: value'")
        key = key.strip().strip("\"'")
        if key in result:
            fail(f"{source}:{lineno}: duplicate key {key!r}")
        rest = rest.strip()
        pos += 1
        if rest:
            result[key] = scalar(rest)
        elif pos < len(lines) and lines[pos][0] > ind:
            result[key], pos = _parse_block(lines, pos, lines[pos][0], source)
        elif pos < len(lines) and lines[pos][0] == ind and isinstance(lines[pos][1], str) and lines[pos][1].startswith("- "):
            result[key], pos = _parse_seq(lines, pos, ind, source)
        else:
            result[key] = None
    return result, pos


def _parse_seq(lines, pos, indent, source):
    result: list = []
    while pos < len(lines):
        ind, content, lineno = lines[pos]
        if ind < indent or not isinstance(content, str) or not (content == "-" or content.startswith("- ")):
            if ind > indent:
                fail(f"{source}:{lineno}: unexpected indentation in sequence")
            break
        if ind > indent:
            fail(f"{source}:{lineno}: unexpected indentation")
        item = content[1:].strip()
        pos += 1
        if not item:
            if pos < len(lines) and lines[pos][0] > ind:
                value, pos = _parse_block(lines, pos, lines[pos][0], source)
            else:
                value = None
            result.append(value)
            continue
        key, sep, rest = item.partition(":")
        if sep and (not rest or rest.startswith(" ")) and not item.startswith(("'", '"', "[", "{")):
            # mapping item: first key inline, following keys indented at ind+2
            sub_indent = ind + 2
            synthetic = [(sub_indent, item, lineno)]
            j = pos
            while j < len(lines) and lines[j][0] >= sub_indent:
                synthetic.append(lines[j])
                j += 1
            value, used = _parse_map(synthetic, 0, sub_indent, source)
            if used != len(synthetic):
                fail(f"{source}:{synthetic[used][2]}: unexpected indentation in sequence mapping")
            result.append(value)
            pos = j
        else:
            result.append(scalar(item))
    return result, pos


def load_file(path) -> object:
    from pathlib import Path

    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        fail(f"required file is missing: {p}")
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"cannot read {p}: {exc}")
    return loads(text, str(p))
