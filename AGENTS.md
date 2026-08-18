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

## PR 合并规程（合并 ≠ 结束，必须跟完整条 CD + Worker 发行）

合并 PR 到 `main` 后，**不能止步于合并**，必须完成以下全流程：

### 1. 合并方式

- 遵循仓库惯例：**squash 合并**，提交标题用 `feat/fix/docs: <标题> (#PR号)` 单行格式（main 历史无 "Merge pull request" 提交）。
- 优先用 GitHub API（`PUT /repos/sumiim/ThreadForge/pulls/<N>/merge`，`merge_method=squash`，`commit_title` 带 `(#N)`）。

### 2. 跟完整条 CD（Continuous Delivery）

合并后 CD 链路为：`push main` → **CI**（`ci.yml`，含 Worker version gate）→ 成功后自动触发 **CD**（`cd.yml`，`workflow_run` 门控，部署控制面到 production）。

- 合并后立即查询 `actions/runs?head_sha=<合并提交>`，确认 **CI 已触发**。
- **等待 CI 全绿**（不要提前收尾）；CI 失败时定位并报告，必要时由 PR 作者修复后重跑。
- CI 成功 → 确认 **CD deploy job 已触发且成功**（SSH 部署控制面）。
- CD 未自动触发时，可用 `workflow_dispatch` 手动触发。

### 3. 新的 Worker 发行 → 必须触发

Worker 发行**不会随合并自动触发**（`worker-release.yml` 只监听 `worker-v*` 标签推送），所以：

- 对比合并前后 `local-worker/pyproject.toml` 的 `project.version`：
  - 版本升高（且该版本尚无 `worker-v<版本>` 标签）→ 需要触发发行。
  - 注意：若中间的版本被跳过（如 0.3.52 无标签），只对当前 main 的版本打标签，历史被跳过版本不要补发。
- 在**包含该版本的合并提交**（即合并后的 main HEAD）上打 annotated tag 并推送：

  ```sh
  git tag -a worker-v<新版本> -m "Worker release v<新版本>"
  git push origin worker-v<新版本>
  ```

- 监控 **Worker Release** workflow 直到完成：validate（标签 == pyproject 版本）→ build wheel → 构建安装包 → smoke test → 签名 manifest → 发布到 production 服务器（`publish-worker`）。
- 任何一步失败都要报告，不要静默结束。

### 4. 收尾检查清单

- [ ] PR 已标记 merged（API 确认 `state=closed` 且 `merged_at` 非空）
- [ ] CI 全绿
- [ ] CD deploy 成功
- [ ] Worker 版本有变化 → `worker-v<版本>` 标签已推送、Worker Release 跑完且发布成功；无变化 → 说明无需发行
- [ ] 本地 `main` 已同步到合并提交