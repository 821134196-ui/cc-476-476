"""HTTP 服务：静态页面、照片目录与 REST API（标准库实现，零依赖）。"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import services as svc
from .adapters import DeliveryAdapter, PaymentAdapter
from .db import Database
from .reminders import ReminderEngine

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "web")
PHOTO_DIR = os.path.join(BASE_DIR, "photos")

_ROUTE_RE = re.compile(
    r"^/api/(?P<col>orders|rooms|windows|pieces|packages|deliveries|repairs|damages|"
    r"incidents|reminders|fees|demo)(?:/(?P<id>\d+))?(?:/(?P<action>[a-z\-]+))?$")


class AppContext:
    def __init__(self, db_path: str):
        os.makedirs(PHOTO_DIR, exist_ok=True)
        self.db = Database(db_path)
        self.svc = svc.Service(self.db)
        self.delivery = DeliveryAdapter()
        self.payment = PaymentAdapter()
        self.reminders = ReminderEngine(self.db)


def create_app(ctx: AppContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CurtainCare/1.0"

        def log_message(self, fmt, *args):
            pass  # 安静运行；错误单独打印

        # --- 静态资源 ---
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path.startswith("/api/"):
                    self._api_get(path)
                elif path.startswith("/photos/"):
                    self._serve_photo(path)
                else:
                    self._serve_static(path)
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": f"服务器错误: {e}"})

        def do_POST(self):
            try:
                self._api_post(urlparse(self.path).path)
            except svc.DomainError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._json(500, {"error": f"服务器错误: {e}"})

        def _serve_static(self, path: str):
            if path in ("", "/"):
                path = "/index.html"
            target = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
            if not target.startswith(STATIC_DIR) or not os.path.isfile(target):
                self._json(404, {"error": "not found"})
                return
            ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                     ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(
                os.path.splitext(target)[1], "application/octet-stream")
            with open(target, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_photo(self, path: str):
            name = os.path.basename(path)
            target = os.path.join(PHOTO_DIR, name)
            if not os.path.isfile(target):
                self._json(404, {"error": "照片不存在"})
                return
            ext = os.path.splitext(name)[1].lower()
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".svg": "image/svg+xml", ".webp": "image/webp"}.get(ext, "application/octet-stream")
            with open(target, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # --- API ---
        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                raise svc.DomainError("请求体不是合法 JSON")

        def _actor(self, data: dict) -> str:
            return data.get("actor", "staff")

        def _api_get(self, path: str):
            if path == "/api/state":
                state = ctx.svc.get_state()
                state["adapter"] = {
                    "delivery_fail_next": ctx.delivery.fail_next or "",
                    "payment_fail_next": ctx.payment.fail_next or "",
                }
                self._json(200, state)
                return
            m = _ROUTE_RE.match(path)
            if m and m.group("col") == "reminders":
                self._json(200, {"reminders": ctx.svc.get_state()["reminders"]})
                return
            self._json(404, {"error": "未知接口"})

        def _api_post(self, path: str):
            s = ctx.svc
            data = self._read_json()
            actor = self._actor(data)

            # 照片上传走二进制（path 以 /photo 结尾）
            if (m := re.match(r"^/api/pieces/(\d+)/photo$", path)):
                self._upload_photo(int(m.group(1)), data)
                return

            m = _ROUTE_RE.match(path)
            if not m:
                self._json(404, {"error": "未知接口"})
                return
            col, rid_s, action = m.group("col"), m.group("id"), m.group("action")
            rid = int(rid_s) if rid_s else None

            result = self._dispatch(col, rid, action, data, actor)
            self._json(200, result if isinstance(result, dict) else {"ok": True, "result": result})

        def _dispatch(self, col, rid, action, d, actor) -> dict:
            s = ctx.svc
            # 建单/结构
            if col == "orders" and action is None and rid is None:
                oid = s.create_order(d["customer_name"], d["phone"], d["address_alias"],
                                     d["address_detail"], d.get("appointment_at"),
                                     actor=actor, note=d.get("note", ""))
                return {"id": oid}
            if col == "orders" and action == "start-removal":
                s.start_removal(rid, actor); return {}
            if col == "orders" and action == "transport":
                s.transport_order(rid, actor); return {}
            if col == "orders" and action == "receive":
                s.receive_order(rid, actor); return {}
            if col == "orders" and action == "postpone":
                s.postpone_order(rid, int(d.get("days", 1)), d.get("reason", ""), actor)
                return {}
            if col == "orders" and action == "deliver":
                batch = s.create_delivery(rid, [int(x) for x in d.get("package_ids", [])],
                                          ctx.delivery, actor)
                return {"batch_no": batch}
            if col == "orders" and action == "close":
                s.close_order(rid, actor); return {}
            if col == "orders" and action == "pay":
                r = s.pay(rid, d.get("method", "微信支付"), ctx.payment, actor)
                return r
            if col == "orders" and action == "appointment":
                s.set_appointment(rid, d["appointment_at"], actor); return {}

            if col == "rooms" and action is None and rid is None:
                return {"id": s.create_room(int(d["order_id"]), d["name"])}
            if col == "windows" and action is None and rid is None:
                return {"id": s.add_window(int(d["room_id"]), d["label"])}
            if col == "pieces" and action is None and rid is None:
                return {"id": s.add_piece(int(d["window_id"]), d["fabric"],
                                          int(d.get("hooks", 0)),
                                          bool(d.get("washable", True)),
                                          d.get("wash_req", ""), d.get("code"), actor)}

            # 拆卸链路
            if col == "pieces" and action == "count-hooks":
                s.count_hooks(rid, int(d.get("hooks", 0)), actor); return {}
            if col == "windows" and action == "seal-bag":
                return {"seal_no": s.seal_window_bag(rid, actor)}
            if col == "pieces" and action == "misbag":
                s.flag_misbag(rid, d.get("found_seal", ""), d.get("note", ""), actor)
                return {}

            # 洗护
            if col == "pieces" and action == "wash":
                s.start_wash(rid, actor); return {}
            if col == "pieces" and action == "skip-wash":
                s.skip_wash(rid, actor, d.get("note", "")); return {}
            if col == "pieces" and action == "wash-finish":
                s.finish_wash(rid, actor); return {}
            if col == "pieces" and action == "dry":
                s.start_drying(rid, actor); return {}
            if col == "pieces" and action == "dry-finish":
                s.finish_drying(rid, actor); return {}
            if col == "pieces" and action == "ready":
                s.mark_ready(rid, actor); return {}
            if col == "pieces" and action == "incident":
                iid = s.report_incident(rid, d["type"], d.get("note", ""), actor,
                                        bool(d.get("resolved", False)))
                return {"incident_id": iid}

            # 损伤 / 赔付 / 修补
            if col == "pieces" and action == "damage":
                did = s.register_damage(rid, d.get("description", ""), actor)
                return {"damage_id": did}
            if col == "pieces" and action == "repair":
                rpid = s.open_repair(rid, d.get("note", ""), actor)
                return {"repair_id": rpid}
            if col == "damages" and action == "decide":
                s.decide_damage(rid, d["decision"], actor, float(d.get("amount", 0)),
                                d.get("note", ""))
                return {}
            if col == "repairs" and action == "finish":
                s.finish_repair(rid, actor); return {}

            # 配包 / 配送
            if col == "windows" and action == "pack":
                return {"seal_no": s.pack_window(rid, actor)}
            if col == "packages" and action == "seal":
                s.seal_package(rid, actor); return {}
            if col == "deliveries" and action == "confirm":
                s.deliver_batch(rid, ctx.delivery, actor); return {}

            # 复装
            if col == "pieces" and action == "reinstall":
                s.scan_reinstall(rid, int(d.get("hooks", 0)), actor,
                                 d.get("photo_path")); return {}
            if col == "pieces" and action == "uneven":
                iid = s.flag_uneven(rid, d.get("note", ""), actor)
                return {"incident_id": iid}
            if col == "incidents" and action == "resolve":
                s.resolve_incident(rid, d.get("note", ""), actor); return {}
            if col == "windows" and action == "accept":
                s.accept_window(rid, actor); return {}

            # 费用
            if col == "fees" and action is None and rid is None:
                fid = s.add_fee(int(d["order_id"]), d["item"], float(d["amount"]),
                                d.get("kind", "charge"), actor)
                return {"id": fid}
            if col == "orders" and action == "review-fees":
                s.review_fees(rid, actor); return {}

            if col == "reminders" and action == "ack":
                with ctx.db.tx() as con:
                    con.execute("UPDATE reminders SET acked=1 WHERE id=?", (rid,))
                return {}

            # 演示用：让下一次适配器调用失败
            if col == "demo" and action == "adapter-fail":
                which = d.get("which", "delivery")
                reason = d.get("reason", "模拟：配送线路临时管制")
                if which == "payment":
                    ctx.payment.fail_next = reason or "模拟：银行通道繁忙"
                else:
                    ctx.delivery.fail_next = reason
                return {}

            raise svc.DomainError(f"未匹配的操作：{path if False else col}/{rid}/{action}", 404)

        def _upload_photo(self, piece_id: int, params: dict):
            kind = params.get("kind", "before")
            if kind not in ("before", "after_wash", "reinstall"):
                raise svc.DomainError("照片类型错误")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise svc.DomainError("空照片")
            if length > 10 * 1024 * 1024:
                raise svc.DomainError("照片过大（>10MB）")
            blob = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "")
            ext = ".jpg"
            if "png" in ctype:
                ext = ".png"
            elif "webp" in ctype:
                ext = ".webp"
            elif "svg" in ctype:
                ext = ".svg"
            name = f"piece{piece_id}-{kind}-{uuid.uuid4().hex[:8]}{ext}"
            with open(os.path.join(PHOTO_DIR, name), "wb") as f:
                f.write(blob)
            rel = f"/photos/{name}"
            with ctx.db.tx() as con:
                p = con.execute("SELECT code FROM pieces WHERE id=?", (piece_id,)).fetchone()
                if p is None:
                    raise svc.DomainError("帘片不存在", 404)
                col = {"before": "photo_before", "after_wash": "photo_after_wash",
                       "reinstall": "photo_reinstall"}[kind]
                con.execute(f"UPDATE pieces SET {col}=? WHERE id=?", (rel, piece_id))
                con.execute("INSERT INTO piece_events(piece_id,kind,actor,detail,at)"
                            " VALUES(?,?,?,?,datetime('now'))",
                            (piece_id, f"photo_{kind}", self._actor(params), rel))
            self._json(200, {"path": rel})

        def _json(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8000,
          open_browser: bool = True) -> None:
    ctx = AppContext(db_path)
    ctx.reminders.start()
    handler = create_app(ctx)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"✓ 家政窗帘拆洗与复装核验系统已启动：{url}")
    print("  按 Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.6, lambda: os.system(f'(xdg-open "{url}" >/dev/null 2>&1 || true)')).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止中…")
    finally:
        ctx.reminders.stop()
        httpd.server_close()
