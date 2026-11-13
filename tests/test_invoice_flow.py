"""整单回填流程的离线测试：混合税率、虚拟行切换、人工插改与失败语义。"""

import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from invoice_controls import InvoiceControls
from invoice_flow import (
    diff_draft,
    plan_invoice,
    process_all,
    process_invoice,
    read_draft,
    verify_invoice,
)


def load_fixture(name='line-items.json'):
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / 'fixtures' / name).read_text())


def controls_in(directory, source, **kwargs):
    path = Path(directory) / 'controls.json'
    controls = InvoiceControls(path, **kwargs)
    return controls


class PlanTests(unittest.TestCase):
    def test_plan_links_discount_and_correction_to_their_groups(self):
        plan = plan_invoice(load_fixture())
        by_ref = {line['reference']: line for line in plan['lines']}
        self.assertEqual(by_ref['D1']['kind'], 'discount')
        self.assertEqual(by_ref['D1']['targets'], ['L1'])
        self.assertEqual(by_ref['D1']['group']['key'], '0.13|excl')
        self.assertEqual(by_ref['C1']['kind'], 'correction')
        self.assertEqual(by_ref['C1']['corrects'], 'L2')
        self.assertEqual(by_ref['C1']['group']['key'], '0.09|incl')
        self.assertEqual(by_ref['C1']['group'], by_ref['L2']['group'])
        self.assertEqual(plan['expected'], {'net': '12.41', 'tax': '1.16', 'gross': '13.57'})

    def test_plan_rejects_dangling_discount_target(self):
        source = {
            'invoice': 'BAD', 'currency': 'CNY', 'rows': [
                {'line': 'L1', 'quantity': '1', 'unit_price': '1', 'tax_rate': '0.1',
                 'tax_included': False},
                {'line': 'D1', 'discount_for': ['L9'], 'amount': '-0.1'},
            ],
        }
        with self.assertRaises(ValueError):
            plan_invoice(source)

    def test_zero_decimal_currency_rounds_to_whole_units(self):
        source = {
            'invoice': 'JPY-1', 'currency': 'JPY', 'rows': [
                {'line': 'L1', 'quantity': '3', 'unit_price': '335', 'tax_rate': '0.1',
                 'tax_included': False},
                {'line': 'L2', 'quantity': '1', 'unit_price': '1005', 'tax_rate': '0.08',
                 'tax_included': True},
            ],
        }
        plan = plan_invoice(source)
        self.assertEqual(plan['config']['quantum'], '1')
        self.assertEqual(plan['expected']['net'], '1936')

    def test_total_rounding_adds_explicit_adjustment_line(self):
        source = {
            'invoice': 'ROUND-1', 'currency': 'CNY', 'rows': [
                {'line': 'L1', 'quantity': '1', 'unit_price': '0.334', 'tax_rate': '0.13',
                 'tax_included': False},
                {'line': 'L2', 'quantity': '1', 'unit_price': '0.334', 'tax_rate': '0.13',
                 'tax_included': False},
            ],
        }
        plan = plan_invoice(source, {'invoice_rounding': 'total'})
        self.assertEqual(plan['rounding']['adjustment_line'], 'ROUND')
        self.assertEqual(plan['rounding']['line_sum_net'], '0.66')
        self.assertEqual(plan['rounding']['invoice_net'], '0.67')
        self.assertEqual(plan['expected']['net'], '0.67')
        rounding_line = plan['lines'][-1]
        self.assertEqual(rounding_line['entry']['net'], '0.01')
        self.assertEqual(rounding_line['group']['key'], 'rounding')

    def test_discount_split_across_tax_groups(self):
        source = {
            'invoice': 'MIX-1', 'currency': 'CNY', 'rows': [
                {'line': 'L1', 'quantity': '10', 'unit_price': '10', 'tax_rate': '0.13',
                 'tax_included': False},
                {'line': 'L2', 'quantity': '2', 'unit_price': '12.45', 'tax_rate': '0.09',
                 'tax_included': True},
                {'line': 'D1', 'discount_for': ['L1', 'L2'], 'amount': '-0.10'},
            ],
        }
        plan = plan_invoice(source)
        discount_lines = [line for line in plan['lines'] if line['kind'] == 'discount']
        self.assertEqual(len(discount_lines), 2)
        self.assertEqual({line['group']['key'] for line in discount_lines},
                         {'0.13|excl', '0.09|incl'})
        total = sum((Decimal(line['entry']['net']) for line in discount_lines), Decimal('0'))
        self.assertEqual(total, Decimal('-0.10'))
        for line in discount_lines:
            self.assertEqual(
                sum((Decimal(v) for v in line['allocations'].values()), Decimal('0')),
                Decimal(line['entry']['net']),
            )
            self.assertEqual(
                sum((Decimal(v) for v in line['source_allocations'].values()), Decimal('0')),
                Decimal('-0.10'),
            )


class EntryTests(unittest.TestCase):
    def test_controls_are_reacquired_after_each_added_line(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            result = process_invoice(source, controls)
            report = result['write_report']
            self.assertTrue(result['completed'])
            # 4 行分两页；控件前缀的 epoch 必须随新增行变化，说明 ID 被重新识别。
            epochs = {
                candidate.split('-')[1]
                for candidate in report['source_line_to_control']['C1'].values()
            }
            self.assertEqual(len(epochs), 1)
            first_page = {c.split('-')[1] for c in report['source_line_to_control']['L1'].values()}
            self.assertNotEqual(first_page, epochs)

    def test_source_line_never_used_as_control_id(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            result = process_invoice(source, controls)
            for write in result['write_report']['writes']:
                self.assertNotIn(write['source_line'], write['control'])
                self.assertTrue(write['control'].startswith('field-'))
            self.assertEqual(set(controls.rows), {'L1', 'L2', 'D1', 'C1'})

    def test_manual_remark_and_confirmed_line_are_preserved(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            controls.add_line('L1')
            controls.add_line('L2')
            controls.manual_edit('L2', 'remark', source['manual_edit']['remark'])
            controls.manual_edit('L1', 'remark', 'human-note')
            draft = read_draft(controls)
            result = process_invoice(source, controls, draft=draft)
            self.assertTrue(result['completed'])
            self.assertEqual(controls.rows['L2']['remark'], 'manual-review')
            self.assertEqual(controls.rows['L1']['remark'], 'human-note')
            preserved = {(item['reference'], item['field']) for item in result['draft_differences']['preserved']}
            self.assertIn(('L2', 'remark'), preserved)
            # 受保护/已有备注不属于机器人写入。
            self.assertFalse([w for w in result['write_report']['writes'] if w['field'] == 'remark'])

    def test_human_edit_after_read_pauses_affected_line_without_overwrite(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            controls.add_line('L1')
            controls.add_line('L2')
            controls.manual_edit('L2', 'remark', 'first-pass')
            draft = read_draft(controls)
            controls.manual_edit('L2', 'remark', 'second-pass')
            result = process_invoice(source, controls, draft=draft)
            self.assertEqual(result['status'], 'paused')
            self.assertFalse(result['completed'])
            self.assertIn('L2', result['paused'])
            conflicts = result['draft_differences']['conflicts']
            self.assertEqual(conflicts[0]['field'], 'remark')
            self.assertEqual(conflicts[0]['actual'], 'second-pass')
            self.assertEqual(controls.rows['L2']['remark'], 'second-pass')
            # 未受影响的 L1 可以写入，但整单不提交。
            written = {w['reference'] for w in result['write_report']['writes']}
            self.assertIn('L1', written)
            self.assertNotIn('L2', written)
            self.assertIsNone(result['post_save'])

    def test_existing_wrong_draft_value_is_reported_not_overwritten(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            controls.add_line('L1')
            controls.manual_edit('L1', 'net', '9.99')
            draft = read_draft(controls)
            result = process_invoice(source, controls, draft=draft)
            self.assertEqual(result['status'], 'paused')
            self.assertEqual(controls.rows['L1']['net'], '9.99')
            self.assertEqual(result['draft_differences']['conflicts'][0]['reason'],
                             'existing-draft-mismatch')
            self.assertTrue(result['pre_save']['errors'])
            self.assertFalse(result['pre_save']['passed'])

    def test_unexpected_extra_line_pauses_invoice(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            controls.add_line('EXTRA')
            controls.manual_edit('EXTRA', 'net', '5')
            result = process_invoice(source, controls)
            self.assertEqual(result['status'], 'paused')
            self.assertIn('EXTRA', result['paused'])
            self.assertEqual(result['draft_differences']['extra_lines'][0]['reason'],
                             'unexpected-line')

    def test_validation_prompt_is_attributed_to_line(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            controls.add_line('L2')
            controls.raise_validation('L2', 'net', 'tax does not match group rate')
            result = process_invoice(source, controls)
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['completed'])
            self.assertEqual(result['write_report']['validation_issues'][0]['reference'], 'L2')
            self.assertEqual(result['write_report']['validation_issues'][0]['field'], 'net')
            self.assertNotIn('L1', {issue['reference']
                                    for issue in result['write_report']['validation_issues']})

    def test_diff_draft_classifies_manual_changes(self):
        source = load_fixture()
        plan = plan_invoice(source)
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            controls.add_line('L1')
            controls.manual_edit('L1', 'remark', 'note')
            snapshot = read_draft(controls)
            controls.manual_edit('L1', 'net', '2.00')
            differences = diff_draft(snapshot, plan, controls)
            reasons = {(item['reference'], item['field']): item['reason']
                       for item in differences['conflicts']}
            self.assertEqual(reasons[('L1', 'net')], 'manual-edit-after-read')
            self.assertEqual(differences['paused'], ['L1'])

class VerificationTests(unittest.TestCase):
    def test_portal_auto_rounding_fails_unless_explicitly_accepted(self):
        source = {
            'invoice': 'HALF-1', 'currency': 'CNY', 'rows': [
                {'line': 'L1', 'quantity': '3', 'unit_price': '0.335',
                 'tax_rate': '0.13', 'tax_included': False},
            ],
        }
        base = {'line_rounding': 'half-even', 'portal_rounding': 'half-up',
                'tax_rule': 'recompute'}
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            strict = process_invoice(source, controls, dict(base))
            self.assertEqual(strict['status'], 'failed')
            self.assertFalse(strict['completed'])
            adjustment = strict['post_save']['portal_adjustments'][0]
            self.assertEqual(adjustment['field'], 'net')
            self.assertEqual(adjustment['type'], 'portal-adjustment')
            self.assertFalse(adjustment['accepted'])
            self.assertFalse(strict['post_save']['rounding']['total_adjustment_explained'])
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            accepted = process_invoice(source, controls, dict(base, accept_portal_adjustments=True))
            self.assertTrue(accepted['completed'])
            self.assertTrue(accepted['post_save']['rounding']['total_adjustment_explained'])

    def test_post_save_mismatch_is_not_reported_complete(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            result = process_invoice(source, controls)
            self.assertTrue(result['completed'])
            # 保存后人工改坏一行，再回读核验：必须失败。
            controls.manual_edit('D1', 'net', '0.00')
            after = verify_invoice(controls, plan_invoice(source), stage='post_save')
            self.assertFalse(after['passed'])
            self.assertEqual(after['discount_allocation'][0]['status'], 'net-difference')
            self.assertTrue(any(error.get('scope') == 'invoice'
                                for error in after['errors']))

    def test_group_reports_explain_discount_and_tax_placement(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            result = process_invoice(source, controls)
            post = result['post_save']
            self.assertEqual(post['groups']['0.13|excl']['lines'], ['L1', 'D1'])
            self.assertEqual(post['groups']['0.09|incl']['lines'], ['L2', 'C1'])
            self.assertEqual(post['discount_allocation'][0]['allocations'], {'L1': '-0.01'})
            self.assertEqual(post['targets_after_discount']['L1']['effective_net_after_discount'],
                             '0.99')
            self.assertEqual(post['targets_after_discount']['L1']['line_net_unchanged'],
                             '1.00')
            self.assertEqual(post['rounding']['line_rounding_effect'],
                             {'net': '-0.00903669724770642201834862',
                              'tax': '0.00338669724770642201834862'})
            self.assertTrue(post['rounding']['total_adjustment_explained'])

    def test_manual_and_automatic_origins_are_distinguishable(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            controls.add_line('L2')
            controls.manual_edit('L2', 'remark', 'manual-review')
            result = process_invoice(source, controls)
            origins = result['post_save']['origins']
            self.assertIn('remark', origins['L2']['manual'])
            self.assertNotIn('remark', origins['L2']['automatic'])
            self.assertIn('net', origins['L1']['automatic'])

    def test_rerun_on_filled_invoice_writes_nothing(self):
        source = load_fixture()
        with TemporaryDirectory() as directory:
            controls = controls_in(directory, source)
            first = process_invoice(source, controls)
            second = process_invoice(source, controls)
            self.assertTrue(first['completed'])
            self.assertTrue(second['completed'])
            self.assertEqual(second['write_report']['writes'], [])
            self.assertEqual(second['draft_differences']['conflicts'], [])


class MultiCurrencyTests(unittest.TestCase):
    def test_multi_currency_invoices_are_independent(self):
        cny = {
            'invoice': 'CNY-1', 'currency': 'CNY', 'rows': [
                {'line': 'L1', 'quantity': '3', 'unit_price': '0.335',
                 'tax_rate': '0.13', 'tax_included': False},
                {'line': 'D1', 'discount_for': ['L1'], 'amount': '-0.01'},
            ],
        }
        jpy = {
            'invoice': 'JPY-1', 'currency': 'JPY', 'rows': [
                {'line': 'L1', 'quantity': '1', 'unit_price': '1005',
                 'tax_rate': '0.08', 'tax_included': True},
            ],
        }
        with TemporaryDirectory() as directory:
            cny_controls = controls_in(directory, cny)
            jpy_controls = InvoiceControls(Path(directory) / 'jpy.json')
            cny_result = process_invoice(cny, cny_controls)
            jpy_result = process_invoice(jpy, jpy_controls)
            self.assertTrue(cny_result['completed'])
            self.assertTrue(jpy_result['completed'])
            self.assertEqual(cny_result['plan']['config']['quantum'], '0.01')
            self.assertEqual(jpy_result['plan']['config']['quantum'], '1')
            self.assertEqual(jpy_controls.rows['L1']['net'], '931')

    def test_process_all_handles_independent_controls(self):
        sources = [load_fixture(), load_fixture()]
        with TemporaryDirectory() as directory:
            def factory(index, source):
                return InvoiceControls(Path(directory) / f'controls-{index}.json')

            results = process_all(sources, factory)
            self.assertEqual([result['status'] for result in results],
                             ['completed', 'completed'])


if __name__ == '__main__':
    unittest.main()
