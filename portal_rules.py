"""纯函数形式的门户结算规则。

控件模拟器与字段计划共用这里的规则，保证“机器人预期”和“门户实际结算”
使用同一套量化/税率口径。这里不读写文件，也不依赖控件。
"""

from decimal import (
    Decimal,
    ROUND_DOWN,
    ROUND_HALF_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
)

ROUNDING_MODES = {
    'half-even': ROUND_HALF_EVEN,
    'half-up': ROUND_HALF_UP,
    'half-down': ROUND_HALF_DOWN,
    'down': ROUND_DOWN,
    'up': ROUND_UP,
}


def decimal_of(value, default='0'):
    if value is None or value == '':
        return Decimal(default)
    return Decimal(str(value))


def quantize(value, quantum='0.01', rounding='half-even'):
    """量化金额，并把 -0.00 归一成 0.00，避免输出带符号的零。"""
    if value is None or value == '':
        return value
    if not isinstance(value, Decimal):
        value = decimal_of(value)
    result = value.quantize(Decimal(str(quantum)), rounding=ROUNDING_MODES[rounding])
    if result == 0:
        result = abs(result)
    return result


def amount_text(value):
    if value is None or value == '':
        return ''
    if not isinstance(value, Decimal):
        value = decimal_of(value)
    return format(value, 'f')


def rate_text(value):
    if value is None or value == '':
        return ''
    return format(decimal_of(value).normalize(), 'f')


def settle_row(row, meta=None, quantum='0.01', tax_rule='quantize', rounding='half-even'):
    """返回门户保存后某一行应有的 net/tax。

    - ``quantize``：门户只按自己的精度量化已录入的金额（原有行为）。
    - ``recompute``：门户按自己的税率规则重算税额。税内含行用
      数量*单价作为含税总额，净额 = 含税总额/(1+税率)；折扣行没有数量，
      按录入净额 * 税率计算。空行不会被凭空补出金额。
    """
    meta = meta or {}
    fields = {field: row.get(field, '') for field in ('net', 'tax')}
    has_content = any(
        row.get(field)
        for field in ('net', 'tax', 'quantity', 'unit_price')
    )
    if tax_rule == 'recompute' and has_content and meta.get('recompute_tax', True):
        rate = decimal_of(row.get('tax_rate') or meta.get('tax_rate') or '0')
        if meta.get('tax_included') and row.get('quantity') and row.get('unit_price'):
            gross = decimal_of(row['quantity']) * decimal_of(row['unit_price'])
            net = gross / (Decimal('1') + rate)
            tax = gross - net
        else:
            if row.get('quantity') and row.get('unit_price'):
                net = decimal_of(row['quantity']) * decimal_of(row['unit_price'])
            else:
                net = decimal_of(row.get('net') or '0')
            tax = net * rate
        fields['net'] = net
        fields['tax'] = tax

    for field in ('net', 'tax'):
        if fields[field] not in (None, ''):
            fields[field] = quantize(fields[field], quantum, rounding)
    return fields
