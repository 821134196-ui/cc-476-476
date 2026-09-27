"""进程内任务提醒：后台守护线程周期扫描洗护超时与预约时限。"""
import threading
import time


class ReminderWorker:
    def __init__(self, service, interval=60):
        self.service = service
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self.history = []  # 进程内触发记录

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="curtain-reminder")
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                fired = self.service.fire_reminders()
                self.history.extend(fired)
            except Exception as exc:  # 提醒线程不能拖垮主进程
                self.history.append({"kind": "worker_error",
                                     "message": str(exc), "level": "error"})

    def tick(self):
        """手动触发一次（测试与演示用）。"""
        fired = self.service.fire_reminders()
        self.history.extend(fired)
        return fired

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
