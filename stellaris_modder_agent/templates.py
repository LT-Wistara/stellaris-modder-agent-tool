"""Anchored templates: substitutions never contribute matching evidence."""
import re
from functools import lru_cache

PLACEHOLDER = re.compile(r'<[^<>\s]+>|enum\[[^\[\]]+\]')
OBJECT = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z')


class Template:
    def __init__(self, name):
        self.name = name
        self.parts = []
        end = 0
        for match in PLACEHOLDER.finditer(name):
            if match.start() > end:
                self.parts.append(('literal', name[end:match.start()].casefold()))
            self.parts.append(('placeholder', match.group()))
            end = match.end()
        if end < len(name):
            self.parts.append(('literal', name[end:].casefold()))
        self.dynamic = any(k == 'placeholder' for k, _ in self.parts)
        self.fixed = sum(len(v.strip('_')) for k, v in self.parts if k == 'literal')
        self.literals = tuple(v for k, v in self.parts if k == 'literal')
        self.pattern = re.compile('^' + ''.join(
            re.escape(v) if k == 'literal' else r'([a-z0-9][a-z0-9_.-]{0,127}?)'
            for k, v in self.parts) + '$', re.I) if self.dynamic else None

    def exact(self, query):
        # A naked <scripted_trigger>, <modifier>, etc. cannot establish existence.
        if not self.dynamic or self.fixed < 3:
            return None
        match = self.pattern.fullmatch(query)
        if match:
            return [{'placeholder': part, 'value': value, 'object_existence': 'UNRESOLVED'}
                    for (_, part), value in zip(
                        [p for p in self.parts if p[0] == 'placeholder'], match.groups())]
        return None

    def partial_score(self, query):
        q = query.casefold()
        if not self.dynamic or not q or len(q) > 256:
            return 0

        @lru_cache(None)
        def solve(index, pos):
            if pos == len(q):
                return (0, False)
            if index == len(self.parts):
                return (-1, False)
            kind, part = self.parts[index]
            if kind == 'literal':
                n = min(len(part), len(q) - pos)
                if part[:n] != q[pos:pos+n]:
                    return (-1, False)
                score = len(part[:n].replace('_', ''))
                if pos + n == len(q):
                    # At least 3 fixed suffix characters after a substitution.
                    return (score, index > 0 and score >= 3)
                tail, anchored = solve(index+1, pos+n)
                return (score+tail, anchored or (index > 0 and score >= 3)) if tail >= 0 else (-1, False)
            best = (-1, False)
            for end in range(pos+1, min(len(q), pos+128)+1):
                if not OBJECT.fullmatch(q[pos:end]):
                    continue
                candidate = solve(index+1, end)
                if candidate > best:
                    best = candidate
            return best

        score, suffix_anchor = solve(0, 0)
        # A generic country_ prefix alone is insufficient. A long fixed prefix
        # or a suffix reached after a substitution is required.
        return score if suffix_anchor or score >= 12 else 0
