# PR-B: packages/harbor-langfuse (LangfusePlugin)

RFC 0001 的第二个 PR：Langfuse 管理面插件（新增包目录 packages/harbor-langfuse/）。

- Dataset 同步：任务 → dataset items（`POST /dataset-items`，稳定 id upsert）
- Experiment 映射：job → experiment；trial 的 ATIF 轨迹经 atif2otel 转换为 OTel
  spans 并富集 `langfuse.*` 属性（experiment/session/tags/item），再由
  LangfuseUploader（PR-A）上传
- 判分回填：`verifier_result.rewards` → `POST /scores`（NUMERIC/CATEGORICAL，
  异常 trial → BOOLEAN harbor_error），稳定 UUID 幂等
- 端点契约取自官方 OpenAPI spec（2026-09-01 快照：/datasets、/dataset-items、/scores）
- 同时更新 docs-mintlify/core-concepts/plugins/existing-plugins.mdx（two→three official plugins + Langfuse 章节）

验证：Python 3.10 + stub harbor，14/14 测试通过（enrich 4 + scores 5 + plugin 5）。
上游 CI（3.12 + workspace 安装）使用真实 harbor 包运行同一测试套件。
