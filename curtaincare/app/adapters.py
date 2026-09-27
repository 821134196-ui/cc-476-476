"""模拟适配器：配送与付款。返回确定性结果，不依赖外部服务。"""
from __future__ import annotations

import itertools
import threading


class DeliveryAdapter:
    """模拟物流：拒单原因可通过 fail_next 注入，其余成功。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._n = itertools.count(1)
        self.fail_next: str | None = None  # 下次 dispatch 的拒单原因

    def dispatch(self, batch_no: str, package_ids: list[int]) -> dict:
        with self._lock:
            if self.fail_next:
                reason = self.fail_next
                self.fail_next = None
                return {"ok": False, "reason": reason, "ref": ""}
            ref = f"SF{next(self._n):06d}"
            return {"ok": True, "ref": ref, "reason": "",
                    "packages": package_ids, "eta_hours": 24}

    def confirm(self, batch_no: str) -> dict:
        return {"ok": True, "ref": f"POD-{batch_no}", "signed": "客户本人"}


class PaymentAdapter:
    """模拟支付：可用 fail_next 制造一次失败结果。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._n = itertools.count(1)
        self.fail_next: str | None = None

    def pay(self, order_id: int, amount: float, method: str) -> dict:
        with self._lock:
            if self.fail_next:
                reason = self.fail_next
                self.fail_next = None
                return {"status": "failed", "reason": reason, "ref": ""}
            return {"status": "success",
                    "ref": f"PAY{next(self._n):08d}",
                    "amount": amount, "method": method}
