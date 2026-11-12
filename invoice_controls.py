import json
from decimal import Decimal
from pathlib import Path


class InvoiceControls:
    fields = ('quantity', 'unit_price', 'tax_rate', 'net', 'tax', 'remark')

    def __init__(self, path, quantum='0.01'):
        self.path = Path(path)
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.quantum = Decimal(quantum)
        self.offset = 0
        self.epoch = 0
        self.controls = {}

    def add_line(self, reference):
        if reference in self.rows:
            raise ValueError('line already exists')
        self.rows[reference] = {field: '' for field in self.fields}
        self.epoch += 1
        self.controls = {}

    def scroll(self, offset):
        self.offset = offset
        self.epoch += 1
        self.controls = {}

    def visible(self):
        self.controls = {}
        visible = []
        for position, reference in enumerate(list(self.rows)[self.offset:self.offset + 2]):
            fields = {}
            for field in self.fields:
                identifier = f'field-{self.epoch}-{position}-{field}'
                self.controls[identifier] = (reference, field)
                fields[field] = {'control': identifier, 'value': self.rows[reference][field]}
            visible.append({'reference': reference, 'fields': fields})
        return visible

    def fill(self, identifier, value):
        if identifier not in self.controls:
            raise ValueError('stale or invisible control')
        reference, field = self.controls[identifier]
        self.rows[reference][field] = value

    def manual_edit(self, reference, field, value):
        self.rows[reference][field] = value
        self.epoch += 1
        self.controls = {}
        self.path.write_text(json.dumps(self.rows))

    def save(self):
        for row in self.rows.values():
            for field in ('net', 'tax'):
                if row[field]:
                    row[field] = str(Decimal(row[field]).quantize(self.quantum))
        self.path.write_text(json.dumps(self.rows))
        return self.totals()

    def totals(self):
        net = sum((Decimal(row['net'] or '0') for row in self.rows.values()), Decimal('0'))
        tax = sum((Decimal(row['tax'] or '0') for row in self.rows.values()), Decimal('0'))
        return {'net': str(net), 'tax': str(tax), 'gross': str(net + tax)}
