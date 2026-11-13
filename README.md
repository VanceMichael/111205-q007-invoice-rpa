# 发票门户明细回填与整单差异核验

## 现有能力

仓库原有 `service.py` 金额规范化、`portal.py` 离线回执门户和
`invoice_controls.py` 两行可见窗口控件模拟器；本次新增以整张发票为单位的
回填流程：

* `invoice_flow.py`：字段计划（源行 -> 门户行、税率分组、折扣/更正关联、
  逐行与整单舍入、预期税额）、草稿回读与差异、控件驱动逐页录入、提交前后
  整单核验。源行号只作业务标识，绝不当作控件 ID；每页都重新读取 `visible()`
  取得的控件。
* `portal_rules.py`：门户结算规则（量化与可选税率重算），由字段计划与控件
  模拟器共用，保证“机器人预期”和“门户实际保存”使用同一口径。
* `demo_flow.py`：合成多币种发票演示（CNY/JPY/USD，含混合税率、折扣跨组、
  负向更正、人工插入行/备注/确认行与读取后改动、以及严格核验失败样例）。

`service.py` 保持原有规范化接口。根目录 `test_service.py` 为原有回归测试，
`tests/test_baseline.py` 同时加载原测试和离线演示验证。

## 环境和运行

使用 Python 3.11.10，仅依赖标准库，无需登录账号、安装第三方包或下载数据。
`.python-version` 固定解释器版本；`pyproject.toml` 仅登记 unittest 入口，
项目不需要打包安装。

在仓库根目录执行：

```sh
python3 -m unittest discover -s tests -v
python3 demo.py
python3 demo_flow.py            # 多币种整单回填演示（JSON）
python3 demo_flow.py --plan     # 只输出各发票的可复核字段计划
python3 invoice_flow.py --source fixtures/line-items.json
```

完整演示命令：

```sh
python3 -m unittest discover -s tests -v && python3 demo.py && python3 demo_flow.py
```

演示实时读取合成输入并运行当前接口，通常在 90 秒以内结束，不回放历史日志。
状态写入临时目录，重复执行不污染仓库；本地 HTTP 示例只访问回环地址，
不连接生产系统。

## 合成来源

`fixtures/line-items.json` 给出单张合成明细及其税额口径；
`fixtures/multicurrency-invoices.json` 提供 CNY/JPY/USD 三张发票和人工
插改场景。`invoice_controls.py` 模拟两行可见窗口，新增与滚动会使旧控件
标识失效；门户保存时按 `portal_rules.py` 的口径量化（可选按自身税率规则
重算），不负责源明细的折扣分摊。旧回执门户 `portal.py` 和样例也保留。

## 字段计划与配置

`plan_invoice(source, config)` 默认：

* `line_rounding = half-even`，`invoice_rounding = sum`（可改 `total`，此时
  生成显式 `ROUND` 调整行并分别报告逐行与整单舍入差额）；
* `discount_allocation = proportional`（可改 `even` / `single`），折扣按被
  指向明细分摊；跨税率组时拆成每组一条独立行，分摊和始终等于源金额；
* 币种决定金额精度（JPY 等零位小数币种按 1 元），可用 `quantum` /
  `portal_quantum` 覆盖；
* `tax_rule` 控制门户保存规则（`quantize` 或 `recompute`）。门户自动调整
  默认不自动接受；已解释且在容差内的调整需显式
  `accept_portal_adjustments = true` 才可计入通过。

## 草稿与人工改动

流程先 `read_draft()` 只读回读草稿。已有非空值、受保护字段（如备注）和已
确认行原样保留；读取后又被人工改动的字段会进入 `draft_differences.conflicts`，
受影响明细进入 `paused`，整单不提交，且绝不覆盖人工值。门户校验提示按明细
记录在 `write_report.validation_issues`，不会触发整单重填。

## 输出语义

`process_invoice()` 只有在提交前后两次整单回读都通过时才返回
`status = completed, passed = true, completed = true`。任何录入失败、门户校验
提示、草稿冲突或核验差异都会返回 `failed` / `paused` 且 `completed = false`；
`post_save.full_row_verification` 之外的分层结果在 `groups`、`discount_allocation`、
`rounding`、`origins`（自动写入 vs 人工修改）中分别列出。

## 能力边界

`invoice_controls.py` 是可替换控件模拟器，不驱动真实浏览器；生产门户的选择器、
登录与异步回执仍需另行实现。当前自动化验证覆盖已有接口、离线响应器和上述
整单回填流程；真实门户的持久化流程决策与跨模块恢复不在此实现范围。

