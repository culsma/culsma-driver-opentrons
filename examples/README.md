# 驱动样例

这些独立样例用于演示移液配置、物料记录和能力检查。每个样例目录包含
`protocol.culs`、`deck.json` 和记录预期结果的 `expected.json`。

| 样例 | 演示内容 |
| --- | --- |
| `multi-source` | 多来源配液及其组分记录 |
| `low-volume-recipe` | 小体积移液、多个来源和显式流速配置 |
| `serial-dilution` | 连续稀释中的中间物料与组分核算 |
| `split-transfer` | 按移液器量程拆分转移，并保持语句的总转移量 |
| `unsupported-separation` | 拒绝配置不支持的磁分离请求 |

## 运行

按[仓库首页](../README.md#install-and-test)安装后，从仓库根目录运行：

```sh
.venv/bin/culsma run examples/low-volume-recipe/protocol.culs \
  --driver opentrons-simulator \
  --driver-config examples/low-volume-recipe/deck.json \
  --artifacts-dir run-artifacts/low-volume-recipe
```

替换样例目录名即可运行其他样例。在输出目录的 `run.json` 中查看执行状态和
设备回执，并与该样例的 `expected.json` 核对。`unsupported-separation` 的
预期结果是能力检查拒绝。

## 完整流程

[全血自动化 benchmark](https://github.com/culsma/culsma/tree/main/benchmarks/automation)
提供完整流程及统一复现入口，已展示配置化的 `high_precision` 移液、
`low_carryover` 换枪头策略、搬板、离心，以及温度和 CO₂ 环境保持。
移液和支持的搬板动作使用官方 Opentrons 模拟器；离心与环境保持使用参考设备模型。
