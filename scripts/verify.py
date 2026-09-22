#!/usr/bin/env python3
"""Run release checks and write a factual, reproducible test report."""
import datetime
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Results(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.passed_ids = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.passed_ids.append(test.id())


def main():
    log = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=Results).run(suite)
    corpus = subprocess.run([sys.executable, str(ROOT / 'stellaris_tool.py'), 'corpus'],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    if corpus.returncode:
        raise SystemExit(corpus.stderr)
    stats = json.loads(corpus.stdout)
    manifest = json.loads((ROOT / 'stellaris_agent/data/UPSTREAM.json').read_text('utf-8'))
    report = {'verified_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'platform': platform.system(), 'python': platform.python_version(),
              'commit_sha': manifest['commit_sha'],
              'CWT_FILES': stats['CWT_FILES'], 'PARSED': stats['PARSED'], 'FAILED': stats['FAILED'],
              'lossless_roundtrip': stats['lossless_roundtrip'], 'symbols': stats['symbols'],
              'startup_seconds': stats['startup_seconds'],
              'TESTS': {'passed': len(result.passed_ids), 'failed': len(result.failures)+len(result.errors),
                        'skipped': len(result.skipped), 'total': result.testsRun},
              'CLI': 'passed' if 'test_tool.ProtocolCase.test_cli_smoke' in result.passed_ids else 'FAILED',
              'MCP': 'started successfully' if 'test_tool.ProtocolCase.test_stdio_all_tools_actual_process' in result.passed_ids else 'FAILED',
              'MCP_HTTP': 'passed' if 'test_tool.ProtocolCase.test_http_transport_all_tools' in result.passed_ids else 'FAILED',
              'passed_tests': result.passed_ids}
    (ROOT / 'docs/TEST_REPORT.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    summary = f"CWT FILES: {stats['CWT_FILES']}\nPARSED: {stats['PARSED']}\nFAILED: {stats['FAILED']}\n\nTESTS:\n{len(result.passed_ids)} passed\n{len(result.failures)+len(result.errors)} failed\n\nCLI: {report['CLI']}\nMCP: {report['MCP']}\nMCP HTTP: {report['MCP_HTTP']}\n"
    (ROOT / 'docs/TEST_REPORT.txt').write_text(summary+'\n'+log.getvalue(), encoding='utf-8')
    print(summary)
    if not result.wasSuccessful():
        print(log.getvalue())
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
