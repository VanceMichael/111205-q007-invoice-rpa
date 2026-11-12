import json
from pathlib import Path
from tempfile import TemporaryDirectory
from invoice_controls import InvoiceControls
from service import invoice_amount


def run():
    fixture = json.loads((Path(__file__).parent / 'fixtures/line-items.json').read_text())
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'invoice.json'
        controls = InvoiceControls(path)
        for row in fixture['rows']:
            controls.add_line(row['line'])
        visible = controls.visible()
        assert [row['reference'] for row in visible] == ['L1', 'L2']
        controls.fill(visible[0]['fields']['net']['control'], str(invoice_amount('1.005')))
        controls.manual_edit('L2', 'remark', fixture['manual_edit']['remark'])
        totals = controls.save()
        controls.scroll(2)
        assert [row['reference'] for row in controls.visible()] == ['D1', 'C1']
        return {'source_line_ids': [row['line'] for row in fixture['rows']], 'visible_line_ids_after_scroll': [row['reference'] for row in controls.visible()], 'portal_entered_totals': totals, 'preserved_remark': InvoiceControls(path).rows['L2']['remark']}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
