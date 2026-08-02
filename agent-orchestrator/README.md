# Agent Orchestrator

ThreadForge 的 LangGraph 编排模块，由 `sumiim/pico-langgraph-harness` 中的可选 LangGraph backend 迁移而来。

当前实现包含：

- Conversation、Read-only 和 Code-change 意图路由。
- Coordinator 主流程。
- Research 只读调研。
- Execute 代码修改。
- Review 只读验收与有界修复循环。
- LangGraph backend adapter 和审计元数据。

Python 导入名暂时保持 `langgraph_pico`，用于兼容旧 CLI 和测试：

```powershell
python -m pip install -e .\pico-legacy-runtime
python -m pip install -e .\agent-orchestrator
```

后续版本再根据稳定的公共合同评估包名迁移，不在首次仓库迁移中同时重写运行时行为。
