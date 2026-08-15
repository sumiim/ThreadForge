# CI 经验总结

## Worker Version Gate

改动了以下路径中的文件时，**必须 bump Worker 版本**（`local-worker/pyproject.toml` + `local-worker/src/threadforge_worker/__init__.py` 同步）：

- `local-worker/pyproject.toml`
- `local-worker/src/`
- `pico-legacy-runtime/pico/`
- `agent-orchestrator/src/`
- `scripts/build-worker-installer.ps1`
- `scripts/worker-installer.nsi`

版本格式：`X.Y.Z`，三个数字语义递增。

## 测试同步

改动接口响应格式时（增加/删除/重命名字段），务必同步更新对应测试的期望值。Worker 协议消息的测试集中在：

- `local-worker/tests/test_worker.py`

搜索 `assert` 找到所有断言位置，对照实际响应更新。

## 常见返工模式

| 场景 | 遗漏 | CI 门控 |
|---|---|---|
| 改 worker 代码 | 版本号没 bump | `Worker version gate` job |
| 改响应字段 | 测试断言没更新 | 对应 job 的 `pytest` 阶段 |

**习惯：改一处时，同步检查版本号和对应测试。**