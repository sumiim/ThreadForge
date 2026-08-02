# Sandbox Workers

ThreadForge 后续版本的不可信工具执行层。

目标是让文件操作、Shell、依赖安装和测试在独立 Docker 容器中执行，并限制挂载、网络、CPU、内存、进程数、超时和环境变量。

该模块尚未实现。当前 Legacy Runtime 的路径校验和审批只属于策略边界，不等同于容器级沙盒。
