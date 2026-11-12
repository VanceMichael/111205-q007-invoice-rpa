from decimal import Decimal, InvalidOperation

def invoice_amount(value, quantum='0.01'):
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid amount') from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError('amount must be finite and non-negative')
    return amount.quantize(Decimal(quantum))
