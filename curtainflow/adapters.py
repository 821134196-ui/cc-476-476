"""模拟适配器：配送与付款。返回确定性结果，不访问任何外部服务。"""
import threading


class DeliveryAdapter:
    """配送公司适配器（模拟）：记录运单并模拟签收。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.shipments = {}

    def create_shipment(self, batch_code, vehicle):
        with self._lock:
            if batch_code in self.shipments:
                raise RuntimeError(f"运单 {batch_code} 已创建")
            tracking = f"TRK{batch_code.replace('-', '')}"
            self.shipments[batch_code] = {
                "tracking_no": tracking,
                "vehicle": vehicle,
                "status": "created",
            }
            return self.shipments[batch_code]

    def confirm(self, batch_code):
        with self._lock:
            if batch_code not in self.shipments:
                # 未登记也允许模拟成功
                self.shipments[batch_code] = {
                    "tracking_no": f"TRK{batch_code.replace('-', '')}",
                    "status": "delivered",
                }
            else:
                self.shipments[batch_code]["status"] = "delivered"
            return {"batch_code": batch_code,
                    **self.shipments[batch_code]}

    def status(self, batch_code):
        return dict(self.shipments.get(batch_code, {}))


class PaymentAdapter:
    """支付通道适配器（模拟）：成功返回交易号，可注入失败用于演示。"""

    def __init__(self, fail_codes=()):
        self._lock = threading.Lock()
        self.txns = []
        self.fail_codes = set(fail_codes)

    def charge(self, order_code, amount):
        with self._lock:
            if order_code in self.fail_codes:
                return {"status": "failed", "txn_no": None,
                        "reason": "模拟支付失败：余额不足"}
            seq = len(self.txns) + 1
            txn_no = f"MOCKPAY-{seq:06d}"
            self.txns.append({"order": order_code, "amount": amount,
                              "txn_no": txn_no})
            return {"status": "success", "txn_no": txn_no, "amount": amount}
