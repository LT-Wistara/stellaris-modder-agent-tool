"""The prompt this server injects into every client session.

``initialize`` returns an ``instructions`` field that a client puts into the
model's system prompt.  It is the only place where the server can tell an agent
*how* to answer, and the failure it guards against is specific: models reach for
a shell by habit, and this project ships a CLI that looks like the obvious thing
to run.  Doing that throws away the parsed index, the call sites and the evidence
statuses -- the agent gets raw text and less certainty for more context.

So the directive is pinned here: it must exist, it must name every tool, and it
must come before the evidence semantics rather than after them.
"""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stellaris_agent.index import Database  # noqa: E402
from stellaris_agent.server import INSTRUCTIONS, TOOLS, Server  # noqa: E402

# A cap, not a target: these instructions ride in every session's system prompt,
# so unbounded growth is a real cost. Keep headroom for editing.
MAX_LENGTH = 3200


class InstructionCase(unittest.TestCase):
    def setUp(self):
        self.text = INSTRUCTIONS
        self.lower = INSTRUCTIONS.casefold()

    def test_every_tool_is_named(self):
        for tool in TOOLS:
            with self.subTest(tool=tool['name']):
                self.assertIn(tool['name'], self.text)

    def test_it_tells_the_agent_not_to_use_the_cli(self):
        self.assertIn('not by running commands', self.lower)
        self.assertIn('cli', self.lower)
        for entry_point in ('stellaris_tool.py', 'start.py'):
            with self.subTest(entry_point=entry_point):
                self.assertIn(entry_point, self.text)

    def test_it_tells_the_agent_not_to_read_the_corpus_or_the_source(self):
        self.assertIn('do not open, grep or read', self.lower)
        self.assertIn('stellaris_agent/data', self.text)

    def test_the_directive_comes_before_the_semantics(self):
        """A rule buried under a wall of detail is a rule that gets skipped."""
        self.assertLess(self.lower.index('not by running commands'),
                        self.text.index('Evidence comes from two sources'))

    def test_editing_the_users_own_files_is_still_allowed(self):
        """The ban is on this server's internals, not on touching the mod."""
        self.assertIn("user's own mod files", self.text)

    def test_the_evidence_vocabulary_is_still_documented(self):
        for status in ('CONFIRMED_CWT', 'CONFIRMED_GAME_DATA', 'TEMPLATE_MATCH',
                       'SUGGESTION', 'UNKNOWN'):
            with self.subTest(status=status):
                self.assertIn(status, self.text)

    def test_it_stays_within_its_budget(self):
        self.assertLess(len(INSTRUCTIONS), MAX_LENGTH,
                        'the injected prompt grew past its budget')

    def test_initialize_returns_it_verbatim(self):
        server = Server(Database())
        result = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                  'params': {'protocolVersion': '2025-06-18'}})['result']
        self.assertEqual(result['instructions'], INSTRUCTIONS)


if __name__ == '__main__':
    unittest.main()
