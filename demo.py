import json
from pathlib import Path
from tempfile import TemporaryDirectory
from portal import InvoicePortal
from service import invoice_amount


def run():
    fixture = json.loads((Path(__file__).parent / 'fixtures/invoices.json').read_text())
    row = fixture['rows'][0]
    normalized = {**row, 'amount': str(invoice_amount(row['amount']))}
    with TemporaryDirectory() as directory:
        path = Path(directory) / 'portal.json'
        portal = InvoicePortal(path, fixture['delay'])
        try:
            portal.submit(normalized, 0, lose_ack=True)
        except TimeoutError:
            pass
        restored = InvoicePortal(path, fixture['delay'])
        reference = restored.find(row['invoice'], row['vendor'])[0]['receipt']
        assert restored.receipt(reference, 0) is None
        receipt = restored.receipt(reference, 5)
        assert receipt['row']['amount'] == '12.34'
        return {'pending_at': 0, 'receipt_at_5': receipt}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
