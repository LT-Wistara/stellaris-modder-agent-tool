"""Lossless Clausewitz/CWT lexer and structural parser.

All characters belong to tokens, including trivia. Nodes reference source spans;
no rewrite or recovery discards unfamiliar text. Declaration spelling remains
opaque to this grammar and is interpreted by the index/semantic layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re


class ParseError(ValueError):
    pass


@dataclass
class Token:
    kind: str
    text: str
    start: int
    end: int
    line: int


@dataclass
class Node:
    key: str
    operator: str | None
    value: str | None
    children: list[Node] | None
    start: int
    end: int
    line: int
    end_line: int
    key_quoted: bool = False
    value_quoted: bool = False
    leading_start: int = 0
    document: Document | None = field(default=None, repr=False)
    parent: Node | None = field(default=None, repr=False)

    @property
    def raw(self):
        return self.document.source[self.start:self.end]

    @property
    def comments(self):
        return self.document.source[self.leading_start:self.start]

    def walk(self):
        yield self
        for child in self.children or []:
            yield from child.walk()

    def field(self, key):
        return next((n for n in self.children or [] if n.key == key), None)


def unquote(text):
    return text[1:-1] if text.startswith('"') and text.endswith('"') else text


def lex(source: str, path="<input>") -> list[Token]:
    tokens = []
    i, line, size = 0, 1, len(source)
    while i < size:
        start, first_line = i, line
        c = source[i]
        if c.isspace() or c == '\ufeff':
            i += 1
            while i < size and source[i].isspace():
                i += 1
            kind = "whitespace"
        elif c == '#':
            while i < size and source[i] not in '\r\n':
                i += 1
            kind = "comment"
        elif c == '"':
            i += 1
            while i < size:
                if source[i] == '\\' and i + 1 < size:
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise ParseError(f"{path}:{first_line}: unterminated quote")
            kind = "string"
        elif c in '{}':
            i += 1
            kind = c
        elif c in '=!?<>' and not (c == '<' and re.match(r'<[^\s<>={}]+>', source[i:])):
            i += 1
            if i < size and (source[i] == '=' or (c == '<' and source[i] == '>')):
                i += 1
            if source[start:i] not in ('=', '==', '!=', '<>', '<', '>', '<=', '>=', '?='):
                raise ParseError(f"{path}:{line}: unsupported operator {source[start:i]!r}")
            kind = "operator"
        else:
            brackets = 0
            while i < size:
                c = source[i]
                if c in '[(':
                    brackets += 1
                elif c in '])':
                    brackets -= 1
                    if brackets < 0:
                        raise ParseError(f"{path}:{line}: unmatched range/declaration delimiter {c}")
                if brackets == 0 and (c.isspace() or c in '{}#=!?'):
                    # A trailing `?` belongs to the key (`colony? = {...}`, an optional
                    # scope link in vanilla job files), while `?=` is still an operator.
                    if c == '?' and not (i + 1 < size and source[i + 1] == '='):
                        i += 1
                        continue
                    break
                # Angle references can occur inside an identifier or bracket declaration.
                if c == '<':
                    match = re.match(r'<[^\s<>={}]+>', source[i:])
                    if match:
                        i += len(match.group())
                        continue
                    if brackets == 0:
                        break
                if c == '>' and brackets == 0:
                    break
                i += 1
            if brackets:
                raise ParseError(f"{path}:{first_line}: unclosed bracket declaration")
            if i == start:
                raise ParseError(f"{path}:{line}: unexpected {source[i]!r}")
            kind = "atom"
        text = source[start:i]
        tokens.append(Token(kind, text, start, i, first_line))
        line += text.count('\n')
    return tokens


@dataclass
class Document:
    source: str
    path: str
    tokens: list[Token]
    nodes: list[Node]

    def walk(self):
        for node in self.nodes:
            yield from node.walk()

    def roundtrip(self):
        return ''.join(token.text for token in self.tokens)


def parse(source: str, path="<input>") -> Document:
    tokens = lex(source, path)
    significant = [t for t in tokens if t.kind not in ('comment', 'whitespace')]
    doc = Document(source, str(path), tokens, [])
    pos = 0

    def fail(message, token=None):
        line = token.line if token else source.count('\n') + 1
        raise ParseError(f"{path}:{line}: {message}")

    def body(nested=False, preceding=0):
        nonlocal pos
        result = []
        while pos < len(significant):
            token = significant[pos]
            if token.kind == '}':
                if not nested:
                    fail('unexpected closing brace', token)
                return result
            if token.kind not in ('atom', 'string', '{'):
                fail('expected key or list value', token)
            pos += 1
            node = Node(unquote(token.text), None, None, None, token.start, token.end,
                        token.line, token.line + token.text.count('\n'), token.kind == 'string',
                        leading_start=preceding, document=doc)
            if token.kind == '{':
                node.key = ''
                node.children = body(True, token.end)
                if pos >= len(significant):
                    fail('unclosed anonymous block', token)
                end = significant[pos]
                pos += 1
                node.end, node.end_line = end.end, end.line
            elif pos < len(significant) and significant[pos].kind == 'operator':
                node.operator = significant[pos].text
                pos += 1
                if pos >= len(significant):
                    fail('missing assignment value', token)
                value = significant[pos]
                pos += 1
                if value.kind == '{':
                    node.children = body(True, value.end)
                    if pos >= len(significant):
                        fail('unclosed block', token)
                    end = significant[pos]
                    pos += 1
                    node.end, node.end_line = end.end, end.line
                elif value.kind in ('atom', 'string'):
                    node.value = unquote(value.text)
                    node.value_quoted = value.kind == 'string'
                    node.end, node.end_line = value.end, value.line + value.text.count('\n')
                else:
                    fail('expected scalar or block', value)
            for child in node.children or []:
                child.parent = node
            result.append(node)
            preceding = node.end
        if nested:
            fail('unclosed block')
        return result

    doc.nodes = body()
    return doc


def parse_file(path: Path, relative=None):
    # read_bytes avoids newline normalization, preserving the shipped corpus exactly.
    return parse(path.read_bytes().decode('utf-8'), relative or str(path))


def metadata(node):
    """Parse CWT semantic comment lines with the same grammar; retain all keys."""
    lines = node.comments.splitlines()
    # A comment on the previous sibling's last line belongs to that sibling.
    if node.leading_start and lines:
        lines = lines[1:]
    text = '\n'.join(line.strip()[2:].strip() for line in lines
                     if line.strip().startswith('##') and not line.strip().startswith('###'))
    if not text:
        return []
    try:
        return parse(text, node.document.path + ':metadata').nodes
    except ParseError:
        # Expose uninterpreted comment syntax rather than losing it.
        return [Node('__unparsed_metadata__', '=', text, None, 0, len(text), node.line,
                     node.line, document=node.document)]


def values(node):
    if node is None:
        return []
    return [n.key for n in node.children] if node.children is not None else [node.value or node.key]


def meta_values(node, key):
    return [value for item in metadata(node) if item.key == key for value in values(item)]
