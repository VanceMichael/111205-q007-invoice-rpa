import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from portal import InvoicePortal


class InvoicePortalTests(unittest.TestCase):
    def test_portal_allows_duplicate_submissions_and_preserves_cancellation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'portal.json'
            portal = InvoicePortal(path, delay=3)
            row = {'invoice': 'SYN1', 'vendor': 'V1', 'currency': 'CNY', 'amount': '1.00'}
            first = portal.submit(row, 0)
            second = portal.submit(row, 0)
            self.assertNotEqual(first, second)
            self.assertEqual(len(portal.find('SYN1', 'V1')), 2)
            self.assertIsNone(portal.receipt(first, 2))
            portal.cancel(first)
            restored = InvoicePortal(path, delay=3)
            self.assertTrue(restored.receipt(first, 3)['cancelled'])
            self.assertFalse(restored.receipt(second, 3)['cancelled'])
