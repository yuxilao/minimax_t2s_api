from __future__ import annotations

import queue
import threading
from typing import Callable, List, Optional, Tuple

from .. import tasks as tasks_mod
from ..errors import APIError


class TaskController:
    """任务中心业务控制器（tk-free，可单测）。

    所有耗时操作（刷新/下载打包/重试）在专用 daemon 线程串行执行；
    结果通过 post(kind, payload) 回调给视图层：
      - ("event", StageEvent)：操作过程事件
      - ("task_op", (ok, message))：操作结果
      - ("tasks_changed", None)：记录有变化，视图应刷新列表
    """

    def __init__(self, store_path: str, pcfg_getter: Callable[[], dict],
                     post: Callable[[str, object], None]) -> None:
        self.store = tasks_mod.TaskStore(store_path)
        self._pcfg_getter = pcfg_getter
        self._post = post
        self._q: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 主线程接口
    def list_records(self) -> List[tasks_mod.TaskRecord]:
        return self.store.list()

    def get(self, task_id: str) -> Optional[tasks_mod.TaskRecord]:
        return self.store.get(task_id)

    def request_refresh(self, task_ids: Optional[List[str]] = None) -> None:
        """task_ids 为 None -> 刷新全部非终态记录。"""
        self._q.put(("refresh", task_ids))

    def request_finalize(self, task_id: str) -> None:
        self._q.put(("finalize", str(task_id)))

    def request_retry(self, task_id: str) -> None:
        self._q.put(("retry", str(task_id)))

    def request_delete(self, task_id: str) -> None:
        self._q.put(("delete", str(task_id)))

    # ------------------------------------------------------------ 工作线程
    def _loop(self) -> None:
        while True:
            op, arg = self._q.get()
            try:
                self._run(op, arg)
            except Exception as e:
                self._post("task_op", (False, str(e)))
            finally:
                self._post("tasks_changed", None)

    def _run(self, op: str, arg) -> None:
        on_event = lambda ev: self._post("event", ev)
        if op == "refresh":
            recs = None
            if arg:
                recs = [r for r in (self.store.get(t) for t in arg) if r is not None]
            n = tasks_mod.refresh_records(self.store, self._pcfg_getter(), recs, on_event=on_event)
            self._post("task_op", (True, f"刷新完成：{n} 条状态更新"))
        elif op == "finalize":
            rec = self._must_get(arg)
            tasks_mod.finalize_record(self.store, rec, self._pcfg_getter(), on_event=on_event)
            self._post("task_op", (True, f"任务 {arg} 下载打包完成"))
        elif op == "retry":
            rec = self._must_get(arg)
            tasks_mod.retry_record(self.store, rec, self._pcfg_getter(), on_event=on_event)
            self._post("task_op", (True, f"任务已重新提交，新 task_id={rec.task_id}"))
        elif op == "delete":
            ok = self.store.remove(arg)
            self._post("task_op", (True, "记录已删除" if ok else "记录不存在"))
        else:
            self._post("task_op", (False, f"未知操作: {op}"))

    def _must_get(self, task_id: str) -> tasks_mod.TaskRecord:
        rec = self.store.get(task_id)
        if rec is None:
            raise APIError(f"任务记录不存在: {task_id}", "api")
        return rec
