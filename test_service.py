import unittest
from decimal import Decimal
from service import invoice_amount

class InvoiceRpaTests(unittest.TestCase):
    def test_quantizes_amount(self):
        self.assertEqual(invoice_amount('12.345'), Decimal('12.34'))

    def test_rejects_negative(self):
        with self.assertRaises(ValueError):
            invoice_amount('-1')


if __name__ == '__main__':
    unittest.main()
