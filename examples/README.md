# Simulator 扩展样例

这些是基于 Culsma benchmark 操作形态改编的独立、可运行样例，不表示完整生物协议已被设备支持。
核心 benchmark：[Culsma benchmark modules](https://github.com/culsma/culsma/tree/b331b1edfaef57b7722536cbca896ed5a7649713/benchmarks/modules)。
每个目录有 `protocol.culs`、`deck.json` 和独立期望 `expected.json`。

| 样例 | Benchmark 依据 | 验证维度 | 独立预期 |
|---|---|---|---|
| multi-source | Module03 的 RIPA + inhibitor 多源配液形态；本例改为 P300 可用的 50/100uL | 一条 Mutation 内多个来源、身份与成分 | 剩余 150/200uL，混合液 150uL（FIRST=50、SECOND=100） |
| low-volume-recipe | 01-cell-transfection S4 的 2/2/2/4uL RNP 配液；本例只表达液体组分，不声称 RNP 生物反应 | P20、四来源、显式流速 | 剩余 2/2/2/4uL，目标 10uL，4 枪头 |
| serial-dilution | example-04 的分级取液/稀释形态；改为 tube 串联，未覆盖 plate/range/loop | 中间混合物再取液、成分守恒 | stock=300、first=200、second≈300uL；second STOCK=100/3、DILUENT=800/3 |
| split-transfer | Module03 的 1mL 冲洗等超 P300 单冲程形态；本例 610uL，未包含过滤或冲洗效果 | 显式 balanced_split、冲程量程与语句总量守恒 | 3 次约 203.333uL；source=390、target=610uL |
| unsupported-separation | example-03 的 magnetic_program 形态；简化为 BUFFER 明确 fate | 程序类型能力拒绝，不启动 vendor | host setup exit 2，DRV_OT_SEPARATION_UNSUPPORTED，无 vendor-config |

成功样例命令（从本项目目录运行）：

```bash
.venv/bin/culsma run examples/low-volume-recipe/protocol.culs \
  --driver opentrons-simulator \
  --driver-config examples/low-volume-recipe/deck.json \
  --artifacts-dir run-artifacts/low-volume-recipe
```

其他样例替换目录名。`run.json` 中 STEP_COMPLETED/STEP_FAILED 的 driver_payload 包含完整回执。
`transfers` 是计划冲程，不是实测量；`action_results` 是虚拟 API 观察。
`returned` 表示调用正常返回；`completion_unknown` 表示开始的调用异常，不能确认其效果；
`not_started` 表示尚未调用。动作 ID 只在一个运行内唯一。

`single` 默认拒绝大于量程的 aliquot；配置 `balanced_split` 才允许分次。
每冲程用新枪头，不能自动合并跨来源动作或偷偷启用 tip reuse。
P20 必须搭配 20uL 枪头架；P300 搭配 300uL 枪头架；配置错误在启动时拒绝。

完整 benchmark 中的 plate/range、2D series、循环绑定、whole-container transfer、
high_precision、thermal/co2 环境、imaging、magnetic/filtration 等尚未覆盖。
浮点体积按容差核对，同时检查组分独立预期，不仅比较模拟器自己生成的日志。
