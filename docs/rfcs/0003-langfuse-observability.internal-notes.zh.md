# RFC 0003 内部审阅备注（非上游提交内容）

> 本文件仅供内部评审，**不得**随 RFC 提交到 `harbor-framework/harbor`。
> 对应英文版：`0003-langfuse-observability.md`；中文翻译版：`0003-langfuse-observability.zh.md`。

## 定位

本 RFC 合并此前讨论的两条线——① Langfuse 插件化（原 PR-A 上传器 + PR-B 管理面插件）；② LiteLLM Proxy 模型链路遥测（TTFT/TPOT/token/cost 真值，零 agent 改动）。方案 B（agent 内打点）仅在 Non-goals 提了一句"ATIF `step.extra` 可承载，留待未来 RFC"，未展开。

## 已模糊化内容（自研未公开，全部泛化处理）

- 具体轨迹质量指标（冗余率/死循环/错误分类等）→ 只写"用户自定义离线派生指标，走同一 Scores API，具体定义 out of scope"
- 三段拆解的特定 score 命名 → 未出现，只在 proxy 部分写了"时间归因于模型/非模型段"的通用描述
- E1–E7 / 现象标签 / 双轨六维等内部体系 → 完全未出现
- demo/原型仓链接 → 未出现（RFC 0001 里有，这版删了，避免暴露内部指标讨论）

## 与 RFC 0001 的关系

本篇取代 0001 的 Langfuse 部分；0001 保留作为接口分析底稿，不重复提交。

## 编号变更说明（2026-09-02）

- 上游 `rfcs/` 已存在 `0002-simulated-users.md`，故本 RFC 由 0002 改编号为 **0003**；提交时仍需向 maintainer 确认下一个可用编号。
- 原英文版文末的中文附录已剥离为本文件；英文版已按干净上游基线（`harbor-framework/harbor` main @ `6af8d6e`）复核并修正接口表述。

## 待拍板事项

- ① PR-C 的 join 工具放插件包内还是独立脚本（RFC 里写了两可，倾向问 maintainer）；
- ② 代理观察者效应的表述力度（当前写法把 +40ms 泛化为 "tens of milliseconds"，未引具体基准）；
- ③ 标题已点出 "Black-Box Agents" 卖点（Part 3 对社区最独特的价值），如不同意可回退。

## 提交前检查清单

- [ ] 从干净上游克隆拉分支（当前干净基线：WSL `/root/harbor-clean`；`/root/harbor-upstream` 是混入我们代码的工作副本，勿当基线）
- [ ] 实测确认 LiteLLM 当前版本会把解析出的 session id 传播到 OTel span 属性（§6.2 已加 implementation note）
- [ ] 确认 Harbor 是否有 agent 环境变量注入机制；若有，可把 §6.1 的措辞从"部署侧配置"改回"Harbor env-injection"
- [ ] 与 maintainer 确认编号（0003 或下一个可用号）
