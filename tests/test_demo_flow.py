import json
import subprocess
import sys
import unittest
from pathlib import Path

import demo_flow


class MultiCurrencyDemoTests(unittest.TestCase):
    def _decodable(self, value):
        return json.loads(json.dumps(value))

    def test_demo_is_repeatable_for_every_currency(self):
        first = demo_flow.run()
        self.assertEqual(first, demo_flow.run())
        statuses = {entry['invoice']: entry['status'] for entry in first['invoices']}
        self.assertEqual(statuses, {
            'SYN-CNY-1': 'completed',
            'SYN-JPY-1': 'completed',
            'SYN-USD-1': 'completed',
        })

    def test_demo_shows_human_edits_and_a_failure(self):
        result = demo_flow.run()
        self.assertEqual(result['scenarios'][0]['status'], 'completed')
        self.assertEqual(result['scenarios'][0]['conflicts'], [])
        self.assertTrue(result['scenarios'][0]['preserved_fields'])
        paused = result['scenarios'][1]
        self.assertEqual(paused['status'], 'paused')
        self.assertFalse(paused['completed'])
        self.assertFalse(paused['submitted'])
        self.assertEqual(paused['post_save_passed'], None)
        self.assertEqual(result['failure_example']['status'], 'failed')
        self.assertFalse(result['failure_example']['completed'])
        self.assertTrue(result['failure_example']['portal_adjustments'])

    def test_demo_cli_matches_in_process_result(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, 'demo_flow.py'], cwd=root, text=True,
            capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), json.loads(json.dumps(demo_flow.run())))

    def test_demo_plan_option_lists_every_portal_line(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, 'demo_flow.py', '--plan'], cwd=root, text=True,
            capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plans = json.loads(result.stdout)
        self.assertEqual([plan['invoice'] for plan in plans],
                         ['SYN-CNY-1', 'SYN-JPY-1', 'SYN-USD-1'])
        for plan in plans:
            self.assertTrue(plan['lines'])
            self.assertTrue(plan['expected_total']['net'])


if __name__ == '__main__':
    unittest.main()
