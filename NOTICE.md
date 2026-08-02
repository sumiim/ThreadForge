# 来源与迁移说明

ThreadForge 首次仓库迁移基于以下代码与设计来源：

- `sumiim/pico-langgraph-harness`
  - 来源地址：https://github.com/sumiim/pico-langgraph-harness
  - 迁移基准 commit：`255dcf3`
  - 迁移内容：Pico Runtime、CLI、测试、评测、文档和 LangGraph backend。
- `htxoffical/pico`
  - 来源地址：https://gitee.com/htxoffical/pico
  - ThreadForge 中的 Pico Legacy Runtime 延续其 AgentLoop、Prompt、Memory、Session、Checkpoint 和基础工具设计。
- `earendil-works/pi`
  - 来源地址：https://github.com/earendil-works/pi
  - 后续四工具与可扩展 Agent 设计参考其公开架构思想；首次迁移未复制该仓库代码。

首次迁移未包含原仓库的 `.git`、本地 `.env`、运行缓存、测试临时目录或其他未跟踪文件。

各来源项目的著作权及许可归其各自权利人所有。对外发布或分发前应继续核对并保留所有适用的许可证文本和版权声明。
