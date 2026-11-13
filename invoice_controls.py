import json
from decimal import Decimal
from pathlib import Path

from portal_rules import settle_row


class InvoiceControls:
    """可替换控件模拟器：两行可见窗口，新增行/滚动/人工修改都会换发控件 ID。"""

    fields = ('quantity', 'unit_price', 'tax_rate', 'net', 'tax', 'remark')

    def __init__(self, path, quantum='0.01', tax_rule='quantize', meta=None,
                 rounding='half-even'):
        self.path = Path(path)
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.quantum = Decimal(quantum)
        self.tax_rule = tax_rule
        self.meta = {key: dict(value) for key, value in (meta or {}).items()}
        self.rounding = rounding
        self.offset = 0
        self.epoch = 0
        self.controls = {}
        self.manual_fields = {}
        self.confirmed = set()
        self.fill_log = []
        self.validation_issues = {}

    def add_line(self, reference, meta=None):
        if reference in self.rows:
            raise ValueError('line already exists')
        self.rows[reference] = {field: '' for field in self.fields}
        if meta:
            self.meta[reference] = dict(meta)
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
        self.fill_log.append({'reference': reference, 'field': field, 'value': value,
                              'control': identifier, 'epoch': self.epoch})

    def manual_edit(self, reference, field, value):
        self.rows[reference][field] = value
        self.manual_fields.setdefault(reference, set()).add(field)
        self.epoch += 1
        self.controls = {}
        self.path.write_text(json.dumps(self.rows))

    def confirm(self, reference):
        """人工确认过的行；回填时必须原样保留。"""
        if reference not in self.rows:
            raise ValueError('line does not exist')
        self.confirmed.add(reference)
        self.epoch += 1
        self.controls = {}

    def raise_validation(self, reference, field, message):
        """模拟门户校验提示；提示必须归属到具体明细，不触发整单重填。"""
        self.validation_issues.setdefault(reference, []).append({
            'field': field, 'message': message,
        })

    def clear_validation(self):
        self.validation_issues = {}

    def save(self):
        for reference, row in self.rows.items():
            settled = settle_row(row, self.meta.get(reference), self.quantum,
                                 self.tax_rule, self.rounding)
            for field in ('net', 'tax'):
                if settled[field] not in (None, ''):
                    row[field] = str(settled[field])
        self.path.write_text(json.dumps(self.rows))
        return self.totals()

    def totals(self):
        net = sum((Decimal(row['net'] or '0') for row in self.rows.values()), Decimal('0'))
        tax = sum((Decimal(row['tax'] or '0') for row in self.rows.values()), Decimal('0'))
        return {'net': str(net), 'tax': str(tax), 'gross': str(net + tax)}
