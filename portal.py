import json
from pathlib import Path


class InvoicePortal:
    def __init__(self, path, delay=5):
        self.path = Path(path)
        self.delay = delay
        self.records = json.loads(self.path.read_text()) if self.path.exists() else []

    def submit(self, row, now, lose_ack=False):
        receipt = {'receipt': 'R' + str(len(self.records) + 1), 'row': dict(row), 'ready_at': now + self.delay, 'cancelled': False}
        self.records.append(receipt)
        self.path.write_text(json.dumps(self.records))
        if lose_ack:
            raise TimeoutError('submission accepted; reply lost')
        return receipt['receipt']

    def find(self, invoice, vendor):
        return [dict(record) for record in self.records if record['row']['invoice'] == invoice and record['row']['vendor'] == vendor]

    def receipt(self, reference, now):
        record = next(record for record in self.records if record['receipt'] == reference)
        return dict(record) if now >= record['ready_at'] else None

    def cancel(self, reference):
        record = next(record for record in self.records if record['receipt'] == reference)
        record['cancelled'] = True
        self.path.write_text(json.dumps(self.records))
