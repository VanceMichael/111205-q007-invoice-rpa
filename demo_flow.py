"""整单回填演示：合成多币种发票 + 离线控件模拟器。

演示内容：

* 每张发票先生成可复核的字段计划（源行 -> 门户行、税率组、折扣分摊、预期税额）；
* 录入时展示分页可见行和实际控件 ID，说明源行号与控件 ID 是两套标识；
* 覆盖混合税率、折扣跨组、负向更正、含税/不含税、零位小数币种；
* 人工插入行、人工备注、已确认行、读取后再次改动，展示保留与暂停；
* 门户按自身规则重算导致核验失败时，结果明确为 failed，而不是“整单完成”。
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

from invoice_controls import InvoiceControls
from invoice_flow import (
    plan_invoice,
    process_invoice,
    process_scenarios,
)

ROOT = Path(__file__).resolve().parent


def load_fixture():
    return json.loads((ROOT / 'fixtures' / 'multicurrency-invoices.json').read_text())


def invoice_plan(source):
    plan = plan_invoice(source)
    return {
        'invoice': plan['invoice'],
        'currency': plan['currency'],
        'lines': [{
            'source_line': line['source_line'],
            'portal_reference': line['reference'],
            'kind': line['kind'],
            'group': line['group']['key'],
            'entry': line['entry'],
            'expected': line['expected'],
            'allocations': line['allocations'],
        } for line in plan['lines']],
        'groups': plan['groups'],
        'rounding': {
            'policy': plan['rounding']['invoice_policy'],
            'line_sum': {'net': plan['rounding']['line_sum_net'],
                         'tax': plan['rounding']['line_sum_tax']},
            'invoice_total': {'net': plan['rounding']['invoice_net'],
                              'tax': plan['rounding']['invoice_tax']},
            'adjustment_line': plan['rounding']['adjustment_line'],
            'line_rounding_effect': plan['rounding']['line_rounding_effect'],
        },
        'expected_total': plan['expected'],
    }


def summarize_run(invoice, result, controls):
    post = result['post_save']
    summary = {
        'invoice': invoice,
        'status': result['status'],
        'completed': result['completed'],
        'submitted': result['completed'],
        'paused': result['paused'],
        'conflicts': [
            {key: item.get(key) for key in ('reference', 'field', 'planned', 'actual', 'reason')}
            for item in result['draft_differences']['conflicts']
        ],
        'preserved_fields': [
            {key: item.get(key) for key in ('reference', 'field', 'value', 'reason')}
            for item in result['draft_differences']['preserved']
        ],
        'pages': [
            {'reference': page['reference'], 'controls': page['controls']}
            for page in result['write_report']['pages']
        ],
        'writes': result['write_report']['writes'],
        'added_lines': result['write_report']['added_lines'],
        'reacquired_controls': result['write_report']['reacquired'],
        'pre_save_passed': result['pre_save']['passed'],
        'post_save_passed': post['passed'] if post else None,
        'groups': post['groups'] if post else None,
        'discount_allocation': post['discount_allocation'] if post else None,
        'rounding_check': post['rounding'] if post else None,
        'origins': post['origins'] if post else None,
        'portal_adjustments': post['portal_adjustments'] if post else None,
        'page_rows_after_run': {reference: dict(row) for reference, row in controls.rows.items()},
    }
    return summary


def run():
    fixture = load_fixture()
    invoices = fixture['invoices']
    by_invoice = {source['invoice']: source for source in invoices}

    output = {
        'invoices': [],
        'scenarios': [],
        'failure_example': None,
    }

    with TemporaryDirectory() as directory:
        for index, source in enumerate(invoices):
            path = Path(directory) / f'invoice-{index}.json'
            controls = InvoiceControls(path)
            plan = invoice_plan(source)
            result = process_invoice(source, controls)
            entry = summarize_run(source['invoice'], result, controls)
            entry['plan'] = plan
            output['invoices'].append(entry)

        # 人工插改场景：读取草稿前先有人填备注/确认行/插行，读取后再改一次。
        scenario_inputs = []
        for index, scenario in enumerate(fixture.get('portal_scenarios', [])):
            source = by_invoice[scenario['invoice']]
            plan = plan_invoice(source)
            scenario_inputs.append({
                **scenario,
                'name': scenario.get('name') or scenario['invoice'],
                'source': source,
                # 已确认行必须由人先填完整，否则属于“确认行不完整”而非回填范围。
                'confirmed_entries_from_plan': list(scenario.get('confirmed', [])),
                'seed_lines': [line['reference'] for line in plan['lines']],
                'post_read_edits': (
                    [scenario['post_read_edit']] if scenario.get('post_read_edit') else []
                ),
            })

        def factory(index, source):
            return InvoiceControls(Path(directory) / f'scenario-{index}.json')

        for result in process_scenarios(scenario_inputs, factory):
            controls = result['scenario_controls']
            summary = summarize_run(result['scenario'], result, controls)
            output['scenarios'].append(summary)

        # 失败示例：门户用自己的舍入规则重算，严格核验不允许自动调整，
        # 保存成功也不能宣称整单完成。
        strict_source = {
            'invoice': 'SYN-STRICT-1',
            'currency': 'CNY',
            'rows': [{
                'line': 'S1', 'sku': 'S', 'quantity': '3',
                'unit_price': '0.335', 'tax_rate': '0.13', 'tax_included': False,
            }],
        }
        strict_controls = InvoiceControls(Path(directory) / 'strict.json')
        strict = process_invoice(
            strict_source, strict_controls,
            {'line_rounding': 'half-even', 'portal_rounding': 'half-up',
             'tax_rule': 'recompute', 'accept_portal_adjustments': False},
        )
        failed = summarize_run('SYN-STRICT-1', strict, strict_controls)
        failed['reason'] = strict['errors']
        output['failure_example'] = failed

    return output


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--plan':
        fixture = load_fixture()
        print(json.dumps([invoice_plan(source) for source in fixture['invoices']],
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))
