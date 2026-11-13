"""整张发票的回填编排：字段计划 -> 控件驱动录入 -> 提交前后整单核验。

设计约束（对应业务需求）：

* 源行 ``line`` 只是稳定业务标识；门户控件 ID 在新增行/滚动后全部重排，
  因此每次操作前都重新读取 ``visible()``，绝不缓存或复用控件 ID。
* 折扣行、负向更正行通过 ``discount_for`` / ``corrects`` 关联明细；折扣按
  配置分摊，并按被指向明细的税率分组拆成独立可见行，避免被挪到别的税率组。
* 先生成可供复核的字段计划与预期税额，再录入；提交前后各回读一次整单。
* 草稿中已有值、已确认行和受保护字段不覆盖；读取草稿后又被人工改动的字段
  输出字段差异，并暂停受影响明细，不重新整单覆盖。
* 门户保存时的自动调整与人工修改分开记录；硬差异不会被“保存成功”掩盖。
"""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from invoice_controls import InvoiceControls
from portal_rules import (
    ROUNDING_MODES,
    amount_text,
    decimal_of,
    quantize,
    rate_text,
    settle_row,
)

CURRENCY_QUANTUM = {
    'JPY': '1',
    'KRW': '1',
    'CLP': '1',
    'ISK': '1',
    'VND': '1',
    'KWD': '0.001',
    'BHD': '0.001',
    'OMR': '0.001',
    'USD': '0.01',
    'EUR': '0.01',
    'CNY': '0.01',
    'GBP': '0.01',
}

DEFAULT_CONFIG = {
    'line_rounding': 'half-even',
    'invoice_rounding': 'sum',          # sum | total
    'invoice_rounding_mode': 'half-up',
    'invoice_rounding_adjust': True,    # total 模式下是否补舍入调整行
    'discount_allocation': 'proportional',  # proportional | even | single
    'discount_basis': 'net',            # net | gross（gross 仅对含税目标有意义）
    'tax_rule': 'quantize',             # 门户保存规则：quantize | recompute
    'portal_rounding': 'half-even',
    'accept_portal_adjustments': False,
    'portal_adjustment_tolerance': None,  # 缺省为一倍金额精度
}

_MONEY_FIELDS = ('net', 'tax')
_ENTRY_FIELDS = ('quantity', 'unit_price', 'tax_rate', 'net', 'tax', 'remark')
_DIFF_FIELDS = ('quantity', 'unit_price', 'tax_rate', 'net', 'tax', 'remark')


# ---------------------------------------------------------------------------
# 基础计算
# ---------------------------------------------------------------------------

def _config(source, config):
    result = dict(DEFAULT_CONFIG)
    result.update(config or {})
    currency = source.get('currency', 'CNY')
    default_quantum = CURRENCY_QUANTUM.get(currency, source.get('rounding_quantum', '0.01'))
    if 'quantum' not in result:
        result['quantum'] = source.get('rounding_quantum') or default_quantum
    if 'portal_quantum' not in result:
        result['portal_quantum'] = result['quantum']
    # 提前校验枚举，避免运行到一半才发现配置无效。
    try:
        Decimal(str(result['quantum']))
        Decimal(str(result['portal_quantum']))
    except Exception as exc:
        raise ValueError('quantum must be a decimal precision') from exc
    if result['portal_adjustment_tolerance'] is None:
        result['portal_adjustment_tolerance'] = str(
            Decimal(result['portal_quantum']) * Decimal('2')
        )
    if result['line_rounding'] not in ROUNDING_MODES:
        raise ValueError(f"unknown line_rounding {result['line_rounding']!r}")
    if result['portal_rounding'] not in ROUNDING_MODES:
        raise ValueError(f"unknown portal_rounding {result['portal_rounding']!r}")
    if result['invoice_rounding_mode'] not in ROUNDING_MODES:
        raise ValueError(f"unknown invoice_rounding_mode {result['invoice_rounding_mode']!r}")
    if result['invoice_rounding'] not in ('sum', 'total'):
        raise ValueError(f"unknown invoice_rounding {result['invoice_rounding']!r}")
    if result['discount_allocation'] not in ('proportional', 'even', 'single'):
        raise ValueError(f"unknown discount_allocation {result['discount_allocation']!r}")
    if result['discount_basis'] not in ('net', 'gross'):
        raise ValueError(f"unknown discount_basis {result['discount_basis']!r}")
    if result['tax_rule'] not in ('quantize', 'recompute'):
        raise ValueError(f"unknown tax_rule {result['tax_rule']!r}")
    return result


def _group_key(rate, tax_included):
    return f"{rate_text(rate)}|{'incl' if tax_included else 'excl'}"


def _raw_item(qty, price, rate, included):
    gross = qty * price
    if included:
        net = gross / (Decimal('1') + rate)
        tax = gross - net
    else:
        net = gross
        tax = net * rate
    return net, tax


def _item_expected(qty, price, rate, included, quantum, rounding_mode):
    """逐行舍入口径。

    不含税行：净额先按精度舍入，税额按舍入后的净额计算再舍入。
    含税行：含税总额先舍入，净额舍入后，税额取“舍入总额 - 净额”，
    保证该行的净额+税额=含税总额（不因分别舍入而丢一分钱）。
    """
    gross_raw = qty * price
    if included:
        net = quantize(gross_raw / (Decimal('1') + rate), quantum, rounding_mode)
        gross = quantize(gross_raw, quantum, rounding_mode)
        tax = gross - net
    else:
        net = quantize(gross_raw, quantum, rounding_mode)
        tax = quantize(net * rate, quantum, rounding_mode)
        gross = net + tax
    return net, tax, gross


def _is_item(row):
    """正常明细：有数量/单价，且不是折扣或更正行。"""
    return (
        'quantity' in row
        and 'unit_price' in row
        and not row.get('discount_for')
        and not row.get('corrects')
    )


def _allocate(exact_total, targets, weights, quantum, mode):
    """把折扣金额精确地分摊到目标（分摊和必须等于源金额）。"""
    if not targets:
        return {}
    total_weight = sum(weights, Decimal('0'))
    if total_weight == 0:
        weights = [Decimal('1') for _ in targets]
        total_weight = Decimal(len(targets))
    shares = {}
    allocated = Decimal('0')
    for index, (target, weight) in enumerate(zip(targets, weights)):
        if index == len(targets) - 1:
            shares[target] = exact_total - allocated
        else:
            share = quantize(exact_total * weight / total_weight, quantum, mode)
            shares[target] = share
            allocated += share
    residual = exact_total - sum(shares.values(), Decimal('0'))
    # 上面的循环已保证最后一项吃差额；再防御一次，确保分摊和恒等于源金额。
    if residual != 0:
        shares[targets[-1]] += residual
    return shares


# ---------------------------------------------------------------------------
# 字段计划
# ---------------------------------------------------------------------------

def plan_invoice(source, config=None):
    """生成整张发票的字段计划、税率分组和预期税额（只读源数据）。"""
    cfg = _config(source, config)
    quantum = cfg['quantum']
    line_rounding = cfg['line_rounding']
    allocation = cfg['discount_allocation']
    basis = cfg['discount_basis']
    protected = {
        key: list(value) for key, value in (source.get('protected_fields') or {}).items()
    }

    source_rows = [dict(row) for row in source.get('rows', [])]
    by_line = {row['line']: row for row in source_rows if row.get('line')}

    lines = []
    raw_net = Decimal('0')
    raw_tax = Decimal('0')
    legacy_net = Decimal('0')   # 未舍入的明细原始合计（仅用于量化整单口径）
    legacy_tax = Decimal('0')
    item_net = {}
    item_group = {}

    def add_group_entry(group, net, tax, reference):
        groups.setdefault(group, {
            'key': _group_key(*group),
            'rate': group[0],
            'tax_included': group[1],
            'lines': [],
            'expected_net': Decimal('0'),
            'expected_tax': Decimal('0'),
        })
        groups[group]['lines'].append(reference)
        groups[group]['expected_net'] += net
        groups[group]['expected_tax'] += tax

    groups = {}

    # 先计算所有正常明细，供折扣按净额分摊。
    for row in source_rows:
        if not _is_item(row):
            continue
        rate = decimal_of(row.get('tax_rate') or '0')
        included = bool(row.get('tax_included'))
        qty = decimal_of(row['quantity'])
        price = decimal_of(row['unit_price'])
        net_raw, tax_raw = _raw_item(qty, price, rate, included)
        net, tax, gross = _item_expected(qty, price, rate, included, quantum, line_rounding)
        entry_net = net
        line = _make_line(
            row, row['line'], 'item', entry={
                'quantity': row['quantity'], 'unit_price': row['unit_price'],
                'tax_rate': rate_text(rate), 'net': amount_text(entry_net),
                'tax': amount_text(tax), 'remark': '',
            }, expected={'net': net, 'tax': tax}, group=(rate, included),
            cfg=cfg, protected=protected,
        )
        line['expected_gross'] = amount_text(gross)
        line['raw'] = {'net': amount_text(net_raw), 'tax': amount_text(tax_raw)}
        lines.append(line)
        item_net[row['line']] = net_raw
        item_group[row['line']] = (rate, included, net, tax)
        raw_net += net_raw
        raw_tax += tax_raw
        legacy_net += net_raw
        legacy_tax += tax_raw
        add_group_entry((rate, included), net, tax, row['line'])

    # 折扣与更正行：保留独立可见行，并归入被指向明细的税率组。
    for row in source_rows:
        if _is_item(row):
            continue
        if row.get('discount_for'):
            _plan_discount(row, by_line, item_net, item_group, quantum, line_rounding,
                           allocation, basis, cfg, protected, lines, groups)
        elif row.get('corrects') or row.get('amount') is not None:
            _plan_correction(row, by_line, item_group, quantum, line_rounding, basis,
                             cfg, protected, lines, groups)
        else:
            raise ValueError(f"line {row.get('line')!r} has no amount or target")

    # 整单舍入：按配置选择“逐行求和”或“按整单原始合计舍入”。
    line_net = sum((decimal_of(line['expected']['net']) for line in lines), Decimal('0'))
    line_tax = sum((decimal_of(line['expected']['tax']) for line in lines), Decimal('0'))
    rounding = {
        'line_rounding': line_rounding,
        'invoice_policy': cfg['invoice_rounding'],
        'invoice_rounding_mode': cfg['invoice_rounding_mode'],
        'quantum': quantum,
        'raw_net': amount_text(raw_net),
        'raw_tax': amount_text(raw_tax),
        'line_sum_net': amount_text(line_net),
        'line_sum_tax': amount_text(line_tax),
        'invoice_net': amount_text(line_net),
        'invoice_tax': amount_text(line_tax),
        'difference_net': '0',
        'difference_tax': '0',
        'adjustment_line': None,
        # 逐行舍入（相对未舍入原始值）只解释明细行；折扣/更正/舍入行是源金额，
        # 不产生额外的逐行舍入残差。
        'line_rounding_effect': {
            'net': amount_text(
                sum((decimal_of(line['expected']['net'])
                     for line in lines if line['kind'] == 'item'), Decimal('0'))
                - raw_net),
            'tax': amount_text(
                sum((decimal_of(line['expected']['tax'])
                     for line in lines if line['kind'] == 'item'), Decimal('0'))
                - raw_tax),
        },
    }
    if cfg['invoice_rounding'] == 'total':
        # 整单口径：用未舍入明细原始合计 + 折扣/更正行的已舍入金额一起量化，
        # 与逐行舍入后再求和分开解释。折扣/更正行本身已是源金额，按已舍入值
        # 参与，避免重复舍入。
        invoice_net = quantize(legacy_net + _non_item_net(lines),
                               quantum, cfg['invoice_rounding_mode'])
        invoice_tax = quantize(legacy_tax + _non_item_tax(lines),
                               quantum, cfg['invoice_rounding_mode'])
        diff_net = invoice_net - line_net
        diff_tax = invoice_tax - line_tax
        rounding.update({
            'invoice_net': amount_text(invoice_net),
            'invoice_tax': amount_text(invoice_tax),
            'difference_net': amount_text(diff_net),
            'difference_tax': amount_text(diff_tax),
        })
        if cfg['invoice_rounding_adjust'] and (diff_net != 0 or diff_tax != 0):
            reference = 'ROUND'
            while reference in by_line or any(line['reference'] == reference for line in lines):
                reference += '1'
            line = {
                'reference': reference,
                'source_line': reference,
                'kind': 'rounding',
                'group': {'key': 'rounding', 'rate': '0', 'tax_included': False},
                'entry': {
                    'quantity': '', 'unit_price': '', 'tax_rate': '',
                    'net': amount_text(diff_net), 'tax': amount_text(diff_tax), 'remark': '',
                },
                'expected': {'net': amount_text(diff_net), 'tax': amount_text(diff_tax)},
                'portal_expected': {'net': amount_text(diff_net), 'tax': amount_text(diff_tax)},
                'protected': [],
                'meta': {'tax_rate': '0', 'tax_included': False, 'recompute_tax': False},
                'allocations': {},
                'expected_gross': amount_text(diff_net + diff_tax),
                'raw': {'net': amount_text(diff_net), 'tax': amount_text(diff_tax)},
            }
            lines.append(line)
            rounding['adjustment_line'] = reference
            rounding['adjusted_net'] = amount_text(invoice_net)
            rounding['adjusted_tax'] = amount_text(invoice_tax)
            group_report_extra = {
                'key': 'rounding',
                'rate': '0',
                'tax_included': False,
                'lines': [reference],
                'expected_net': amount_text(diff_net),
                'expected_tax': amount_text(diff_tax),
            }

    expected_net = sum((decimal_of(line['expected']['net']) for line in lines), Decimal('0'))
    expected_tax = sum((decimal_of(line['expected']['tax']) for line in lines), Decimal('0'))

    group_report = {}
    for group, values in groups.items():
        group_report[_group_key(*group)] = {
            'rate': rate_text(group[0]),
            'tax_included': group[1],
            'lines': values['lines'],
            'expected_net': amount_text(values['expected_net']),
            'expected_tax': amount_text(values['expected_tax']),
        }
    if rounding['adjustment_line']:
        group_report['rounding'] = group_report_extra

    return {
        'invoice': source.get('invoice'),
        'vendor': source.get('vendor'),
        'currency': source.get('currency', 'CNY'),
        'config': {key: cfg[key] for key in (
            'quantum', 'portal_quantum', 'line_rounding', 'invoice_rounding',
            'invoice_rounding_mode', 'invoice_rounding_adjust', 'discount_allocation',
            'discount_basis', 'tax_rule', 'portal_rounding',
            'accept_portal_adjustments', 'portal_adjustment_tolerance',
        )},
        'lines': lines,
        'groups': group_report,
        'discounts': [line for line in lines if line['kind'] == 'discount'],
        'corrections': [line for line in lines if line['kind'] == 'correction'],
        'rounding': rounding,
        'expected': {
            'net': amount_text(expected_net),
            'tax': amount_text(expected_tax),
            'gross': amount_text(expected_net + expected_tax),
        },
    }


def _make_line(row, reference, kind, entry, expected, group, cfg, protected,
               allocations=None, extra=None):
    meta = {
        'tax_rate': rate_text(group[0]),
        'tax_included': group[1],
        # 门户重算只适用于有数量/单价的明细行；折扣、纯金额更正和舍入行
        # 保留计划给出的净额/税额，否则门户会按净额再乘税率而重复计税。
        'recompute_tax': bool(entry.get('quantity') and entry.get('unit_price')),
        'kind': kind,
    }
    portal_entry = settle_row(
        entry, meta, cfg['portal_quantum'], cfg['tax_rule'], cfg['portal_rounding']
    )
    line = {
        'reference': reference,
        'source_line': row.get('line', reference),
        'kind': kind,
        'sku': row.get('sku'),
        'group': {
            'key': _group_key(*group),
            'rate': rate_text(group[0]),
            'tax_included': group[1],
        },
        'entry': dict(entry),
        'expected': {'net': amount_text(expected['net']), 'tax': amount_text(expected['tax'])},
        'portal_expected': {
            'net': amount_text(portal_entry['net']) if portal_entry['net'] not in (None, '') else '',
            'tax': amount_text(portal_entry['tax']) if portal_entry['tax'] not in (None, '') else '',
        },
        'protected': list(protected.get(row.get('line', reference), [])),
        'meta': meta,
        'allocations': dict(allocations or {}),
    }
    if extra:
        line.update(extra)
    return line


def _non_item_net(lines):
    return sum(
        (decimal_of(line['expected']['net']) for line in lines
         if line['kind'] in ('discount', 'correction')),
        Decimal('0'),
    )


def _non_item_tax(lines):
    return sum(
        (decimal_of(line['expected']['tax']) for line in lines
         if line['kind'] in ('discount', 'correction')),
        Decimal('0'),
    )


def _plan_discount(row, by_line, item_net, item_group, quantum, line_rounding, allocation,
                   basis, cfg, protected, lines, groups):
    targets = [target for target in row.get('discount_for') or []]
    if not targets:
        raise ValueError(f"discount {row['line']} has no target")
    unknown = [target for target in targets if target not in item_net]
    if unknown:
        raise ValueError(f"discount {row['line']} targets unknown lines: {unknown}")
    total = decimal_of(row.get('amount'))
    weights = [abs(item_net[target]) for target in targets]
    if allocation == 'single':
        shares = {target: (total if index == 0 else Decimal('0'))
                  for index, target in enumerate(targets)}
    elif allocation == 'even':
        shares = {target: quantize(total / len(targets), quantum, line_rounding)
                  for target in targets}
        shares[targets[-1]] += total - sum(shares.values(), Decimal('0'))
    else:
        shares = _allocate(total, targets, weights, quantum, line_rounding)

    # 同一折扣命中不同税率组时，拆成每组一条独立可见行。分摊为零的组
    # 不生成空行，但完整分摊仍记录在 source_allocations 供复核。
    buckets = {}
    for target in targets:
        rate, included, _, _ = item_group[target]
        buckets.setdefault((rate, included), []).append(target)
    bucket_shares = {
        group: sum((shares[target] for target in group_targets), Decimal('0'))
        for group, group_targets in buckets.items()
    }
    nonzero = [group for group, amount in bucket_shares.items() if amount != 0]
    if not nonzero:
        nonzero = [next(iter(buckets))]

    suffix = 0
    for (rate, included) in nonzero:
        bucket_targets = buckets[(rate, included)]
        suffix += 1
        reference = row['line'] if len(nonzero) == 1 else f"{row['line']}#{suffix}"
        allocated = bucket_shares[(rate, included)]
        if basis == 'gross' and included:
            net_raw = allocated / (Decimal('1') + rate)
            tax_raw = allocated - net_raw
        else:
            net_raw = allocated
            tax_raw = net_raw * rate
        net = quantize(net_raw, quantum, line_rounding)
        tax = quantize(tax_raw, quantum, line_rounding)
        entry = {
            'quantity': '', 'unit_price': '', 'tax_rate': rate_text(rate),
            'net': amount_text(net), 'tax': amount_text(tax), 'remark': '',
        }
        line = _make_line(
            row, reference, 'discount', entry, {'net': net, 'tax': tax},
            (rate, included), cfg, protected,
            allocations={target: amount_text(shares[target]) for target in bucket_targets},
            extra={
                'targets': bucket_targets,
                'source_allocations': {
                    target: amount_text(value) for target, value in shares.items()
                },
                'discount_source_total': amount_text(total),
            },
        )
        lines.append(line)
        groups.setdefault((rate, included), {
            'key': _group_key(rate, included), 'rate': rate, 'tax_included': included,
            'lines': [], 'expected_net': Decimal('0'), 'expected_tax': Decimal('0'),
        })
        groups[(rate, included)]['lines'].append(reference)
        groups[(rate, included)]['expected_net'] += net
        groups[(rate, included)]['expected_tax'] += tax


def _plan_correction(row, by_line, item_group, quantum, line_rounding, basis,
                     cfg, protected, lines, groups):
    target = row.get('corrects')
    target_meta = item_group.get(target)
    if target_meta:
        target_rate, target_included, target_net, target_tax = target_meta
    else:
        target_rate, target_included = Decimal('0'), False
    rate = decimal_of(row.get('tax_rate') or (target_rate if target_meta else '0'))
    included = bool(row.get('tax_included', target_included))
    if row.get('quantity') is not None and row.get('unit_price') is not None:
        qty = decimal_of(row['quantity'])
        price = decimal_of(row['unit_price'])
        net_raw, tax_raw = _raw_item(qty, price, rate, included)
        net, tax, gross = _item_expected(qty, price, rate, included, quantum, line_rounding)
        quantity, unit_price = row['quantity'], row['unit_price']
    else:
        amount = decimal_of(row.get('amount'))
        if basis == 'gross' and included:
            net_raw = amount / (Decimal('1') + rate)
            tax_raw = amount - net_raw
        else:
            net_raw, tax_raw = amount, amount * rate
        net = quantize(net_raw, quantum, line_rounding)
        tax = quantize(tax_raw, quantum, line_rounding)
        quantity, unit_price = '', ''
    entry = {
        'quantity': quantity, 'unit_price': unit_price, 'tax_rate': rate_text(rate),
        'net': amount_text(net), 'tax': amount_text(tax), 'remark': '',
    }
    line = _make_line(
        row, row['line'], 'correction', entry, {'net': net, 'tax': tax},
        (rate, included), cfg, protected,
        extra={'corrects': target} if target else None,
    )
    lines.append(line)
    groups.setdefault((rate, included), {
        'key': _group_key(rate, included), 'rate': rate, 'tax_included': included,
        'lines': [], 'expected_net': Decimal('0'), 'expected_tax': Decimal('0'),
    })
    groups[(rate, included)]['lines'].append(row['line'])
    groups[(rate, included)]['expected_net'] += net
    groups[(rate, included)]['expected_tax'] += tax


# ---------------------------------------------------------------------------
# 草稿读取与差异
# ---------------------------------------------------------------------------

def read_draft(controls, confirmed=None):
    """只读回读门户当前草稿（包含已填字段、受保护字段和已确认行）。"""
    draft = {
        'rows': {reference: dict(row) for reference, row in controls.rows.items()},
        'manual_fields': {
            reference: set(fields) for reference, fields in controls.manual_fields.items()
        },
        'confirmed': set(getattr(controls, 'confirmed', set())),
        'meta': {reference: dict(meta) for reference, meta in controls.meta.items()},
    }
    if confirmed:
        draft['confirmed'].update(confirmed)
    return draft


def diff_draft(snapshot, plan, controls, config=None):
    """比较“读取时草稿”与“录入时页面”，返回保留/差异/暂停明细。

    * 读取时已有非空值且与计划一致：保留。
    * 读取时已有非空值与计划不同：字段差异，暂停该明细（不覆盖）。
    * 读取后人工又改动（值变化、清空、新增行）：字段差异，暂停受影响明细。
    * 受保护字段（如备注）：始终原样保留；读取后被人工改动也报告差异并暂停。
    """
    cfg = dict(config or {})
    planned = {line['reference']: line for line in plan['lines']}
    current = controls.rows
    confirmed = set(getattr(controls, 'confirmed', set())) | set(snapshot.get('confirmed', set()))
    preserve_protected = set(cfg.get('preserve_fields', ('remark',)))

    preserved, conflicts, missing = [], [], []
    for reference, line in planned.items():
        if reference not in current:
            missing.append({'reference': reference, 'type': 'missing-line'})
            continue
        was = snapshot['rows'].get(reference, {})
        now = current.get(reference, {})
        confirmed_row = reference in confirmed
        if confirmed_row:
            # 已确认行整体保留，不写入任何字段。受保护字段（备注等）只保留、
            # 不参与计划比对；其余字段若与计划不同则报确认行差异。
            incomplete = []
            for field in _DIFF_FIELDS:
                actual = now.get(field, '')
                target = line['entry'].get(field, '')
                is_protected = (
                    field in line.get('protected', []) or field in preserve_protected
                )
                if actual in (None, ''):
                    if target not in (None, '') and not is_protected:
                        incomplete.append(field)
                    continue
                preserved.append({
                    'reference': reference, 'field': field, 'value': actual,
                    'reason': 'protected' if is_protected else 'confirmed',
                })
                if not is_protected and target not in (None, '') and actual != target:
                    conflicts.append(_conflict(reference, field, target, actual,
                                               'confirmed-line-mismatch'))
            if incomplete:
                conflicts.append({
                    'reference': reference, 'field': ','.join(incomplete),
                    'planned': 'confirmed line complete', 'actual': 'blank fields',
                    'reason': 'confirmed-line-incomplete',
                })
            continue
        for field in _DIFF_FIELDS:
            initial = was.get(field, '')
            actual = now.get(field, '')
            target = line['entry'].get(field, '')
            is_protected = field in line.get('protected', []) or field in preserve_protected

            if is_protected and actual not in (None, ''):
                # 受保护字段（如备注）原样保留；若读取后又被人工改动则报告差异。
                preserved.append({
                    'reference': reference, 'field': field, 'value': actual,
                    'reason': 'protected',
                })
                if initial not in (None, '') and actual != initial:
                    conflicts.append(_conflict(reference, field, target, actual,
                                               'manual-edit-after-read', protected=True))
                continue

            if actual not in (None, '') and actual != initial:
                # 读取草稿后又被人改动：一律输出字段差异并暂停该明细。
                if actual == target:
                    preserved.append({'reference': reference, 'field': field,
                                      'value': actual, 'reason': 'manual-edit-agrees'})
                else:
                    conflicts.append(_conflict(reference, field, target, actual,
                                               'manual-edit-after-read',
                                               protected=is_protected))
                continue

            if actual in (None, ''):
                continue  # 空白字段留给录入；是否有冲突由上面分支决定。

            if actual == target:
                preserved.append({'reference': reference, 'field': field,
                                  'value': actual, 'reason': 'existing-draft'})
            else:
                conflicts.append(_conflict(reference, field, target, actual,
                                           'existing-draft-mismatch',
                                           protected=is_protected))

    extra = []
    for reference in current:
        if reference not in planned:
            extra.append({
                'reference': reference, 'field': 'line', 'planned': None,
                'actual': current[reference], 'reason': 'unexpected-line',
                'blocking': True,
            })

    paused = sorted({item['reference'] for item in conflicts
                     if item['reference'] in planned and item.get('blocking', True)})
    if extra:
        paused.extend(item['reference'] for item in extra)
    paused = sorted(set(paused))
    return {
        'preserved': preserved,
        'conflicts': conflicts,
        'extra_lines': extra,
        'missing_lines': missing,
        'added_lines': [line['reference'] for line in plan['lines']
                        if line['reference'] not in snapshot['rows']],
        'paused': paused,
    }


def _conflict(reference, field, planned, actual, reason, protected=False, blocking=True):
    result = {
        'reference': reference, 'field': field,
        'planned': planned, 'actual': actual, 'reason': reason,
        'blocking': blocking,
    }
    if protected:
        result['protected'] = True
    return result


# ---------------------------------------------------------------------------
# 控件驱动录入
# ---------------------------------------------------------------------------

def _visible_fields(controls, reference):
    """当前页面重新识别控件；每次调用都会刷新控件 ID。"""
    for page in controls.visible():
        if page['reference'] == reference:
            return page['fields']
    return None


def _locate(controls, reference, known_order):
    """翻页直到目标行可见；返回当次新发下的控件，旧 ID 一律不用。"""
    fields = _visible_fields(controls, reference)
    if fields:
        return fields
    if reference not in known_order:
        known_order = list(controls.rows)
    row_count = len(controls.rows)
    for offset in range(0, max(row_count, 1), 2):
        controls.scroll(offset)
        fields = _visible_fields(controls, reference)
        if fields:
            return fields
    return None


def enter_invoice(controls, plan, draft=None, config=None):
    """按字段计划逐页录入；返回页面、实际写入控件和保留字段。"""
    draft = draft or read_draft(controls)
    differences = diff_draft(draft, plan, controls, config)
    paused = differences['paused']
    planned = {line['reference']: line for line in plan['lines']}
    order = [line['reference'] for line in plan['lines']]

    report = {
        'status': 'entered',
        'pages': [],
        'writes': [],
        'preserved': list(differences['preserved']),
        'conflicts': list(differences['conflicts']),
        'extra_lines': list(differences['extra_lines']),
        'paused': paused,
        'errors': [],
        'source_line_to_control': {},
        'reacquired': 0,
        'added_lines': [],
        'validation_issues': [
            {'reference': reference, **issue}
            for reference, issues in
            getattr(controls, 'validation_issues', {}).items()
            for issue in issues
        ],
    }

    # 行按计划顺序在需要时新增；每次 add_line 后控件 ID 全部失效，
    # 因此随后调用 _locate() 重新读取 visible() 取得新控件，旧 ID 不使用。
    for reference in order:
        if reference in paused:
            report['source_line_to_control'][reference] = None
            continue
        if reference not in controls.rows:
            controls.add_line(reference, planned[reference]['meta'])
            report['added_lines'].append(reference)
        line = planned[reference]
        fields = _locate(controls, reference, order)
        if not fields:
            report['errors'].append({
                'reference': reference, 'field': 'line', 'error': 'line not reachable on page',
            })
            continue
        page = {
            'reference': reference,
            'controls': {field: info['control'] for field, info in fields.items()},
            'visible_values': {field: info['value'] for field, info in fields.items()},
        }
        report['pages'].append(page)
        report['source_line_to_control'][reference] = page['controls']
        for field in _ENTRY_FIELDS:
            target = line['entry'].get(field, '')
            current = controls.rows[reference].get(field, '')
            if current not in (None, '',) or target in (None, ''):
                continue
            identifier = page['controls'].get(field)
            if not identifier:
                continue
            try:
                controls.fill(identifier, target)
            except ValueError as exc:
                # 旧控件失效：重新识别当前页对应控件，只补这一列；
                # 不整单重填，也不把失败提示记到别的明细。
                refreshed = _locate(controls, reference, order)
                replacement = refreshed.get(field)['control'] if refreshed and field in refreshed else None
                if not replacement:
                    report['errors'].append({
                        'reference': reference, 'field': field,
                        'error': f'{exc}; control not re-acquired',
                    })
                    continue
                page['controls'][field] = replacement
                report['reacquired'] = report.get('reacquired', 0) + 1
                try:
                    controls.fill(replacement, target)
                except ValueError:
                    report['errors'].append({
                        'reference': reference, 'field': field,
                        'error': f'{exc}; control not re-acquired',
                    })
                    continue
                identifier = replacement
            report['writes'].append({
                'reference': reference, 'source_line': line['source_line'],
                'field': field, 'value': target, 'control': identifier,
            })

    if report['errors']:
        report['status'] = 'failed'
    elif paused:
        report['status'] = 'paused'
    return report


# ---------------------------------------------------------------------------
# 整单核验
# ---------------------------------------------------------------------------

def verify_invoice(controls, plan, snapshot=None, config=None, stage='post_save', skip=()):
    """回读整单，分层解释分组、折扣分摊、舍入以及门户/人工差异。

    ``skip`` 中的明细因人工草稿差异被暂停，不要求其金额已符合计划；
    它们仍会记入返回值，但不会被误当成整单通过。
    """
    skip = set(skip)
    cfg = dict(config or {})
    if 'accept_portal_adjustments' not in cfg:
        cfg = {**plan['config'], **cfg}
    tolerance = Decimal(str(cfg.get('portal_adjustment_tolerance') or '0'))
    accept_portal = bool(cfg.get('accept_portal_adjustments'))
    planned = {line['reference']: line for line in plan['lines']}
    current = {reference: dict(row) for reference, row in controls.rows.items()}
    manual = getattr(controls, 'manual_fields', {})
    confirmed = set(getattr(controls, 'confirmed', set()))
    if snapshot:
        confirmed |= set(snapshot.get('confirmed', set()))

    errors, line_diffs, accepted = [], [], []
    groups = {}
    for key, group in plan['groups'].items():
        groups[key] = {
            'key': key,
            'rate': group['rate'],
            'tax_included': group['tax_included'],
            'lines': list(group['lines']),
            'expected_net': decimal_of(group['expected_net']),
            'expected_tax': decimal_of(group['expected_tax']),
            'actual_net': Decimal('0'),
            'actual_tax': Decimal('0'),
        }

    for reference, line in planned.items():
        if reference not in current:
            if reference in skip:
                continue
            errors.append({
                'reference': reference, 'field': 'line', 'type': 'missing-line',
                'message': 'planned line absent from portal',
            })
            continue
        row = current[reference]
        group = groups[line['group']['key']]
        group['actual_net'] += decimal_of(row.get('net'))
        group['actual_tax'] += decimal_of(row.get('tax'))
        if reference in skip:
            continue
        for field in _MONEY_FIELDS:
            expected = decimal_of(line['expected'][field])
            actual = decimal_of(row.get(field))
            if expected == actual:
                continue
            delta = actual - expected
            portal_expected = line.get('portal_expected', {}).get(field, '')
            is_manual = (
                field in manual.get(reference, set())
                or (snapshot is not None
                    and snapshot['rows'].get(reference, {}).get(field, '') not in (None, '')
                    and snapshot['rows'].get(reference, {}).get(field, '') != line['entry'][field]
                    and actual == snapshot['rows'][reference].get(field, ''))
                or reference in confirmed and actual != expected
            )
            entry = {
                'reference': reference,
                'source_line': line['source_line'],
                'field': field,
                'group': line['group']['key'],
                'expected': amount_text(expected),
                'portal_expected': portal_expected,
                'actual': amount_text(actual),
                'delta': amount_text(delta),
            }
            if is_manual:
                entry.update({'type': 'manual-deviation', 'accepted': False})
                errors.append(entry)
                line_diffs.append(entry)
                continue
            matches_portal = (
                portal_expected not in (None, '')
                and decimal_of(portal_expected) == actual
            )
            if accept_portal and matches_portal and abs(delta) <= tolerance:
                entry.update({'type': 'portal-adjustment', 'accepted': True})
                accepted.append(entry)
                line_diffs.append(entry)
            else:
                entry.update({
                    'type': 'portal-adjustment' if matches_portal else 'unexplained-mismatch',
                    'accepted': False,
                })
                errors.append(entry)
                line_diffs.append(entry)

    extra_rows = [reference for reference in current if reference not in planned and reference not in skip]
    for reference in extra_rows:
        errors.append({
            'reference': reference, 'field': 'line', 'type': 'unexpected-line',
            'actual': current[reference],
            'message': 'line exists in portal but not in source invoice',
        })

    # 折扣分摊：逐条独立行核对，并汇总到被指向明细。
    discount_report = []
    target_totals = {}
    for line in plan['lines']:
        if line['kind'] != 'discount':
            continue
        actual_net = decimal_of(current.get(line['reference'], {}).get('net'))
        actual_tax = decimal_of(current.get(line['reference'], {}).get('tax'))
        status = 'ok'
        if actual_net != decimal_of(line['expected']['net']):
            status = 'net-difference'
        if actual_tax != decimal_of(line['expected']['tax']):
            status = 'tax-difference'
        discount_report.append({
            'reference': line['reference'],
            'source_line': line['source_line'],
            'group': line['group']['key'],
            'targets': line.get('targets', []),
            'allocations': line['allocations'],
            'expected': line['expected'],
            'actual': {'net': amount_text(actual_net), 'tax': amount_text(actual_tax)},
            'status': status,
        })
        for target, allocated in line['allocations'].items():
            target_totals.setdefault(target, Decimal('0'))
            target_totals[target] += decimal_of(allocated)
    target_after = {}
    for target, allocated in target_totals.items():
        base = planned.get(target)
        if base:
            target_after[target] = {
                'allocated_discount': amount_text(allocated),
                # 折扣是独立可见行，明细行本身的净额不变；这里给出
                # “明细行 + 其折扣行”的合计，便于复核折扣确实落在本明细上。
                'effective_net_after_discount': amount_text(
                    decimal_of(base['expected']['net']) + allocated),
                'line_net_unchanged': amount_text(decimal_of(base['expected']['net'])),
                'actual_line_net': amount_text(decimal_of(current.get(target, {}).get('net'))),
                'note': 'discount stays on its own row; effective total includes it',
            }

    for key, group in groups.items():
        group['expected'] = {
            'net': amount_text(group['expected_net']),
            'tax': amount_text(group['expected_tax']),
        }
        group['actual'] = {
            'net': amount_text(group['actual_net']),
            'tax': amount_text(group['actual_tax']),
        }
        group['delta'] = {
            'net': amount_text(group['actual_net'] - group['expected_net']),
            'tax': amount_text(group['actual_tax'] - group['expected_tax']),
        }
        group['status'] = 'paused' if any(
            reference in skip for reference in group['lines']
        ) else 'ok' if (
            group['actual_net'] == group['expected_net']
            and group['actual_tax'] == group['expected_tax']
        ) else 'difference'
        # 只保留可序列化的文本，Decimal 内部值不进入输出。
        del group['expected_net'], group['expected_tax']
        del group['actual_net'], group['actual_tax']

    actual_total_net = sum((decimal_of(row.get('net')) for row in current.values()), Decimal('0'))
    actual_total_tax = sum((decimal_of(row.get('tax')) for row in current.values()), Decimal('0'))
    expected_net = decimal_of(plan['expected']['net'])
    expected_tax = decimal_of(plan['expected']['tax'])
    expected_lines = [line for line in plan['lines'] if line['reference'] not in skip]
    checked_expected_net = sum(
        (decimal_of(line['expected']['net']) for line in expected_lines), Decimal('0')
    )
    checked_expected_tax = sum(
        (decimal_of(line['expected']['tax']) for line in expected_lines), Decimal('0')
    )
    checked_actual_net = sum(
        (decimal_of(current.get(line['reference'], {}).get('net')) for line in expected_lines),
        Decimal('0'),
    )
    checked_actual_tax = sum(
        (decimal_of(current.get(line['reference'], {}).get('tax')) for line in expected_lines),
        Decimal('0'),
    )
    skipped_total = {
        'net': amount_text(sum((decimal_of(current.get(reference, {}).get('net'))
                                for reference in skip), Decimal('0'))),
        'tax': amount_text(sum((decimal_of(current.get(reference, {}).get('tax'))
                                for reference in skip), Decimal('0'))),
    }
    rounding_report = dict(plan['rounding'])
    rounding_report.update({
        'expected_total': {'net': amount_text(expected_net), 'tax': amount_text(expected_tax)},
        'actual_total': {'net': amount_text(actual_total_net), 'tax': amount_text(actual_total_tax)},
        'checked_total': {
            'expected': {'net': amount_text(checked_expected_net),
                         'tax': amount_text(checked_expected_tax)},
            'actual': {'net': amount_text(checked_actual_net),
                       'tax': amount_text(checked_actual_tax)},
        },
        'paused_total': skipped_total,
        'paused': sorted(skip),
        'total_difference': {
            'net': amount_text(actual_total_net - expected_net),
            'tax': amount_text(actual_total_tax - expected_tax),
        },
        'line_rounding_effect': plan['rounding']['line_rounding_effect'],
    })
    total_adjustment_explained = False
    checked_net_delta = checked_actual_net - checked_expected_net
    checked_tax_delta = checked_actual_tax - checked_expected_tax
    if checked_net_delta == 0 and checked_tax_delta == 0:
        # 没有整单差额，无需解释门户/人工调整。
        total_adjustment_explained = True
    else:
        accepted_net = sum((decimal_of(item['delta']) for item in accepted if item['field'] == 'net'), Decimal('0'))
        accepted_tax = sum((decimal_of(item['delta']) for item in accepted if item['field'] == 'tax'), Decimal('0'))
        total_adjustment_explained = (
            accept_portal
            and checked_net_delta == accepted_net
            and checked_tax_delta == accepted_tax
        )
        if not total_adjustment_explained:
            errors.append({
                'scope': 'invoice',
                'field': 'net' if checked_net_delta != 0 else 'tax',
                'expected': amount_text(checked_expected_net),
                'actual': amount_text(checked_actual_net),
                'type': 'total-mismatch',
            })
    total_incomplete = bool(skip)
    rounding_report['total_adjustment_explained'] = total_adjustment_explained
    rounding_report['total_incomplete'] = total_incomplete

    automatic = {}
    for entry in getattr(controls, 'fill_log', []):
        automatic.setdefault(entry['reference'], set()).add(entry['field'])
    manual_report = {
        reference: sorted(fields) for reference, fields in manual.items()
    }
    origin = {}
    for reference in current:
        written = automatic.get(reference, set())
        touched = manual.get(reference, set())
        origin[reference] = {
            'automatic': sorted(written - touched),
            'manual': sorted(touched),
            'both': sorted(written & touched),
        }

    passed = not errors and not extra_rows and not skip
    return {
        'stage': stage,
        'status': 'passed' if passed else 'failed',
        'passed': passed,
        'complete': passed,
        'paused': sorted(skip),
        'errors': errors,
        'line_diffs': line_diffs,
        'portal_adjustments': [item for item in line_diffs
                               if item['type'] == 'portal-adjustment'],
        'manual_deviations': [item for item in errors
                              if item.get('type') == 'manual-deviation'],
        'groups': {key: value for key, value in groups.items()},
        'discount_allocation': discount_report,
        'targets_after_discount': target_after,
        'rounding': rounding_report,
        'origins': origin,
        'manual_edits': manual_report,
        'row_count': len(current),
        'expected_line_count': len(planned),
    }


# ---------------------------------------------------------------------------
# 整单流程
# ---------------------------------------------------------------------------

def process_invoice(source, controls, config=None, draft=None):
    """整张发票：计划 -> 回读草稿 -> 录入 -> 提交前后回读核验。"""
    cfg = _config(source, config)
    plan = plan_invoice(source, cfg)
    # 让可替换控件与计划使用同一门户规则；真实门户由自身决定，模拟器在此对齐。
    controls.quantum = Decimal(cfg['portal_quantum'])
    controls.tax_rule = cfg['tax_rule']
    controls.rounding = cfg['portal_rounding']
    for line in plan['lines']:
        controls.meta.setdefault(line['reference'], dict(line['meta']))
    draft = draft or read_draft(controls)

    differences = diff_draft(draft, plan, controls, cfg)
    paused = list(differences['paused'])
    write_report = enter_invoice(controls, plan, draft=draft, config=cfg)

    # 即使草稿有冲突，也已录入了未受影响明细；提交前只核验这些行。
    pre_save = verify_invoice(
        controls, plan, snapshot=draft, config=cfg, stage='pre_save', skip=paused
    )

    result = {
        'invoice': plan['invoice'],
        'currency': plan['currency'],
        'plan': plan,
        'draft_differences': differences,
        'paused': sorted(set(paused)),
        'write_report': write_report,
        'pre_save': pre_save,
        'post_save': None,
        'status': 'failed',
        'passed': False,
        'completed': False,
    }

    if paused:
        result['status'] = 'paused'
        result['errors'] = ['manual draft differences: affected lines paused; invoice not submitted']
        result['pre_save']['status'] = 'paused'
        result['pre_save']['passed'] = False
        result['pre_save']['paused'] = sorted(set(paused))
        result['pre_save']['errors'].append({
            'scope': 'invoice', 'type': 'paused-lines',
            'references': sorted(set(paused)),
            'message': 'invoice not submitted because affected lines await review',
        })
        return result

    if write_report['status'] == 'failed':
        result['status'] = 'failed'
        result['errors'] = ['field entry failed; nothing submitted']
        result['pre_save']['status'] = 'failed'
        result['pre_save']['passed'] = False
        return result

    if write_report.get('validation_issues'):
        result['status'] = 'failed'
        result['errors'] = ['portal validation issues; invoice not submitted']
        result['pre_save']['status'] = 'failed'
        result['pre_save']['passed'] = False
        return result

    if not pre_save['passed']:
        result['status'] = 'failed'
        result['errors'] = ['pre-save read-back failed; invoice not submitted']
        return result

    totals = controls.save()
    post_save = verify_invoice(controls, plan, snapshot=draft, config=cfg, stage='post_save')
    post_save['totals'] = totals
    post_save['pre_save_passed'] = True
    result['post_save'] = post_save
    if not post_save['passed']:
        result['status'] = 'failed'
        result['errors'] = ['post-save read-back failed; invoice incomplete']
        return result

    # 保存前草稿中已有值/人工确认行也必须被保留；verify 的 origins 会记录。
    result['status'] = 'completed'
    result['passed'] = True
    result['completed'] = True
    return result


def process_all(sources, controls_factory, config=None, drafts=None):
    """批量处理多张（多币种）发票；逐张独立核验。"""
    results = []
    for index, source in enumerate(sources):
        controls = controls_factory(index, source)
        result = process_invoice(source, controls, config, (drafts or {}).get(source['invoice']))
        results.append(result)
    return results


def process_scenarios(scenarios, controls_factory, config=None):
    """按场景在读取草稿前后施加人工操作，展示保留/暂停/插行差异。

    ``scenarios`` 形如 ``fixtures/multicurrency-invoices.json`` 的
    ``portal_scenarios``：每个场景指定发票、读取草稿前的人工备注、已确认行、
    人工插入行，以及读取后再次人工改动（必须触发暂停）。
    """
    results = []
    for index, scenario in enumerate(scenarios):
        source = scenario['source']
        controls = controls_factory(index, source)
        plan = plan_invoice(source, config)
        planned = {line['reference']: line for line in plan['lines']}
        for reference in scenario.get('seed_lines', list(planned)):
            controls.add_line(reference, planned.get(reference, {}).get('meta'))
        for reference in scenario.get('confirmed_entries_from_plan', []):
            for field, value in planned[reference]['entry'].items():
                if value:
                    controls.manual_edit(reference, field, value)
        for edit in scenario.get('manual_edits', []):
            controls.manual_edit(edit['reference'], edit['field'], edit['value'])
        for reference in scenario.get('confirmed', []):
            controls.confirm(reference)
        for reference in scenario.get('inserted_lines', []):
            controls.add_line(reference)
            controls.manual_edit(reference, 'remark', 'inserted-by-human')
        draft = read_draft(controls)
        for edit in scenario.get('post_read_edits', []):
            controls.manual_edit(edit['reference'], edit['field'], edit['value'])
        result = process_invoice(source, controls, config, draft=draft)
        result['scenario'] = scenario.get('name', scenario['invoice'])
        result['scenario_controls'] = controls
        results.append(result)
    return results


def load_source(path):
    return json.loads(Path(path).read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description='整单发票回填（离线控件模拟器）')
    parser.add_argument('--source', default='fixtures/line-items.json')
    parser.add_argument('--config', default=None, help='JSON 配置文件')
    parser.add_argument('--state', default=None, help='控件状态文件（缺省临时目录）')
    args = parser.parse_args(argv)
    source = load_source(args.source)
    config = load_source(args.config) if args.config else {}
    temporary = None
    if args.state is None:
        import tempfile
        temporary = tempfile.TemporaryDirectory()
        state = Path(temporary.name) / 'controls.json'
    else:
        state = Path(args.state)
    controls = InvoiceControls(state, **{
        key: config[key] for key in ('portal_quantum', 'tax_rule', 'rounding', 'meta')
        if key in config
    })
    result = process_invoice(source, controls, config)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if temporary:
        temporary.cleanup()
    return 0 if result['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
