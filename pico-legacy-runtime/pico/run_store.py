"""运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。
"""

import json
import os
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            self.root.chmod(0o700)

    @staticmethod
    def _secure_dir(path):
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            path.chmod(0o700)

    @staticmethod
    def _fsync_directory(path):
        if sys.platform == "win32":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def run_dir(self, run_id):
        return self.root / _run_id(run_id)

    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"

    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        run_dir = self.run_dir(task_state)
        self._secure_dir(run_dir)
        self.write_task_state(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        self._secure_dir(path.parent)
        self._write_json_atomic(path, task_state.to_dict())
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        self._secure_dir(path.parent)
        # trace 采用 jsonl 追加写入，原因是 agent 运行过程是流式事件序列，
        # 逐条落盘比“最后一次性写整份 trace”更稳，也更适合调试。
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if sys.platform != "win32":
            path.chmod(0o600)
        self._fsync_directory(path.parent)
        return path

    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        self._secure_dir(path.parent)
        self._write_json_atomic(path, report)
        return path

    def load_task_state(self, task_id):
        return json.loads(self.task_state_path(task_id).read_text(encoding="utf-8"))

    def load_report(self, task_id):
        return json.loads(self.report_path(task_id).read_text(encoding="utf-8"))

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。
        # 这样即使中途异常，也不容易留下半截 JSON。
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=path.name + ".",
                suffix=".tmp",
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_with_retry(temp_path, path)
            temp_path = None
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)
        if sys.platform != "win32":
            path.chmod(0o600)
        self._fsync_directory(path.parent)

    @staticmethod
    def _replace_with_retry(temp_path, path):
        """Retry transient Windows sharing violations during atomic replace."""
        delays = (0.01, 0.02, 0.04, 0.08, 0.16)
        for attempt in range(len(delays) + 1):
            try:
                temp_path.replace(path)
                return
            except PermissionError:
                if attempt >= len(delays):
                    raise
                time.sleep(delays[attempt])
