import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from invoice_controls import InvoiceControls


class ControlsTests(unittest.TestCase):
    def test_control_identity_changes_and_portal_rounds_entered_totals(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'invoice.json'
            ui = InvoiceControls(path)
            ui.add_line('L1')
            first = ui.visible()[0]['fields']
            ui.fill(first['net']['control'], '1.005')
            ui.fill(first['tax']['control'], '0.135')
            stale = first['remark']['control']
            ui.add_line('L2')
            with self.assertRaises(ValueError):
                ui.fill(stale, 'wrong row')
            ui.manual_edit('L2', 'remark', 'manual-review')
            total = ui.save()
            self.assertEqual(total, {'net': '1.00', 'tax': '0.14', 'gross': '1.14'})
            self.assertEqual(InvoiceControls(path).rows['L2']['remark'], 'manual-review')
            ui.add_line('L3')
            self.assertEqual(len(ui.visible()), 2)
            ui.scroll(2)
            self.assertEqual(ui.visible()[0]['reference'], 'L3')
