"""HTTP 服务：JSON API + 静态页面 + 本地照片目录。

仅用标准库。角色通过请求头 X-Role 传递（crew/washer/cs/cs_lead/finance）。
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .service import CurtainService, DomainError, ROLES, ROLE_NAMES, INCIDENT_TYPES

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


def make_handler(svc, reminder_worker, photo_dir):
    photo_root = os.path.abspath(photo_dir)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "CurtainFlow/1.0"

        # ---------------- 基础工具 ----------------
        def log_message(self, fmt, *args):  # 精简日志
            pass

        def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _error(self, code, message):
            self._send(code, {"error": message})

        def _role(self):
            role = self.headers.get("X-Role", "crew")
            return role if role in ROLES else "crew"

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return json.loads(raw.decode("utf-8") or "{}"), None
            if "multipart/form-data" in ctype:
                return None, self._multipart(raw, ctype)
            return {}, None

        def _multipart(self, raw, ctype):
            """极简 multipart 解析：提取第一个文件与文本字段。"""
            m = re.search(r"boundary=([^\s;]+)", ctype)
            if not m:
                raise DomainError("缺少 multipart boundary", 400)
            boundary = m.group(1).strip('"').encode()
            parts = raw.split(b"--" + boundary)
            fields, file_part = {}, None
            for part in parts:
                if b"Content-Disposition" not in part:
                    continue
                head, _, data = part.partition(b"\r\n\r\n")
                data = data.rstrip(b"\r\n")
                name_m = re.search(rb'name="([^"]+)"', head)
                fname_m = re.search(rb'filename="([^"]*)"', head)
                if not name_m:
                    continue
                name = name_m.group(1).decode()
                if fname_m and fname_m.group(1):
                    file_part = {"field": name,
                                 "filename": fname_m.group(1).decode(),
                                 "data": data}
                else:
                    fields[name] = data.decode("utf-8", "replace")
            return {"fields": fields, "file": file_part}

        # ---------------- 路由 ----------------
        def do_GET(self):
            try:
                self._route("GET")
            except DomainError as e:
                self._error(e.code, e.message)
            except Exception as e:  # noqa
                self._error(500, f"服务器错误：{e}")

        def do_POST(self):
            try:
                self._route("POST")
            except DomainError as e:
                self._error(e.code, e.message)
            except json.JSONDecodeError:
                self._error(400, "请求体不是合法 JSON")
            except Exception as e:  # noqa
                self._error(500, f"服务器错误：{e}")

        def _route(self, method):
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path.startswith("/photos/"):
                return self._serve_photo(path)
            if path.startswith("/api/"):
                return self._api(method, path, qs)
            return self._serve_static(path)

        def _serve_photo(self, path):
            name = os.path.basename(path)
            full = os.path.join(photo_root, name)
            if not os.path.isfile(full):
                return self._error(404, "照片不存在")
            ext = os.path.splitext(name)[1].lower()
            with open(full, "rb") as f:
                self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

        def _serve_static(self, path):
            if path in ("/", ""):
                path = "/index.html"
            full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
            if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                return self._error(404, "页面不存在")
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as f:
                self._send(200, f.read(), MIME.get(ext, "text/plain"))

        # ---------------- API ----------------
        def _api(self, method, path, qs):
            role = self._role()
            seg = [s for s in path.split("/") if s][1:]  # 去掉 'api'
            s0 = seg[0] if len(seg) > 0 else ""
            s2 = seg[2] if len(seg) > 2 else ""

            def oid(i=1):
                try:
                    return int(seg[i])
                except (IndexError, ValueError):
                    raise DomainError("路径参数缺失或非法", 400)

            if method == "GET":
                if seg == ["meta"]:
                    return self._send(200, {
                        "roles": [{"id": r, "name": ROLE_NAMES[r]} for r in ROLES],
                        "incident_types": [
                            {"id": k, "name": v} for k, v in INCIDENT_TYPES.items()],
                    })
                if seg == ["orders"]:
                    return self._send(200, svc.list_orders())
                if s0 == "orders" and len(seg) == 2:
                    return self._send(200, svc.order_tree(oid()))
                if s0 == "orders" and len(seg) == 3 and s2 == "bags":
                    return self._send(200, svc.list_bags(oid()))
                if s0 == "orders" and len(seg) == 3 and s2 == "packages":
                    return self._send(200, svc.list_packages(oid()))
                if s0 == "orders" and len(seg) == 3 and s2 == "incidents":
                    return self._send(200, svc.list_incidents(oid()))
                if s0 == "orders" and len(seg) == 3 and s2 == "repairs":
                    return self._send(200, svc.list_repairs(oid()))
                if s0 == "orders" and len(seg) == 3 and s2 == "reinstalls":
                    return self._send(200, svc.list_reinstalls(oid()))
                if s0 == "orders" and len(seg) == 3 and s2 == "fees":
                    return self._send(200, svc.fee_review(oid()))
                if seg == ["wash-queue"]:
                    return self._send(200, svc.wash_queue())
                if seg == ["incidents"]:
                    return self._send(200, svc.list_incidents())
                if seg == ["claims"]:
                    status = qs.get("status", [None])[0]
                    return self._send(200, svc.list_claims(status))
                if seg == ["repairs"]:
                    return self._send(200, svc.list_repairs())
                if seg == ["packages"]:
                    return self._send(200, svc.list_packages())
                if seg == ["batches"]:
                    return self._send(200, svc.list_batches())
                if seg == ["reinstalls"]:
                    return self._send(200, svc.list_reinstalls())
                if seg == ["reminders"]:
                    return self._send(200, {
                        "live": svc.due_reminders(),
                        "fired": svc.list_reminders(),
                    })
                return self._error(404, "接口不存在")

            # POST
            body, mp = self._body()
            if mp is not None and s0 == "panels" and s2 == "photo":
                f = mp["file"]
                if not f:
                    raise DomainError("未收到文件", 400)
                ext = os.path.splitext(f["filename"])[1].lower() or ".png"
                kind = mp["fields"].get("kind", "before")
                return self._send(200, svc.upload_photo(oid(), kind, f["data"], ext))
            if body is None:
                body = {}

            if seg == ["orders"]:
                return self._send(200, svc.create_order(
                    body["customer"], body["rooms"],
                    body.get("appoint_at"), body.get("note", "")))
            if s0 == "orders" and s2 == "schedule":
                return self._send(200, svc.schedule_order(oid(), body["appoint_at"]))
            if s0 == "orders" and s2 == "start-dismantle":
                return self._send(200, svc.start_dismantle(oid()))
            if s0 == "orders" and s2 == "postpone":
                return self._send(200, svc.postpone_order(
                    oid(), body["appoint_at"], body.get("reason", ""), role))
            if s0 == "orders" and s2 == "close":
                return self._send(200, svc.close_order(oid(), role))
            if s0 == "orders" and s2 == "pay":
                return self._send(200, svc.pay_order(oid(), role))
            if s0 == "windows" and s2 == "seal":
                return self._send(200, svc.seal_bag(
                    oid(), body["panel_ids"], body.get("seal_no")))
            if s0 == "windows" and s2 == "pack":
                return self._send(200, svc.pack_package(
                    oid(), body["panel_ids"], body.get("seal_no")))
            if s0 == "bags" and s2 == "ship":
                return self._send(200, svc.ship_bag(oid()))
            if s0 == "bags" and s2 == "receive":
                return self._send(200, svc.receive_bag(oid()))
            if s0 == "panels" and s2 == "start-wash":
                return self._send(200, svc.start_wash(oid(), role))
            if s0 == "panels" and s2 == "dry":
                return self._send(200, svc.finish_dry(oid(), role))
            if s0 == "panels" and s2 == "damage":
                return self._send(200, svc.report_damage(
                    oid(), body["type"], body.get("detail", ""), role))
            if s0 == "panels" and s2 == "repair":
                return self._send(200, svc.create_repair(
                    oid(), body.get("description", ""), role))
            if s0 == "repairs" and s2 == "finish":
                return self._send(200, svc.finish_repair(oid(), role))
            if s0 == "incidents" and s2 == "claim":
                return self._send(200, svc.propose_claim(
                    oid(), float(body["amount"]), body.get("reason", ""), role))
            if s0 == "incidents" and s2 == "rework":
                return self._send(200, svc.approve_rework(oid(), role))
            if s0 == "claims" and s2 == "decide":
                return self._send(200, svc.decide_claim(
                    oid(), bool(body.get("approve")), role))
            if seg == ["batches"]:
                return self._send(200, svc.create_batch(
                    body["package_ids"], body.get("vehicle", "沪A·D3721")))
            if s0 == "batches" and s2 == "dispatch":
                return self._send(200, svc.dispatch_batch(oid()))
            if s0 == "batches" and s2 == "deliver":
                return self._send(200, svc.deliver_batch(oid()))
            if s0 == "packages" and s2 == "scan":
                return self._send(200, svc.scan_reinstall(
                    int(body["panel_id"]), oid(), role))
            if s0 == "panels" and s2 == "accept":
                return self._send(200, svc.accept_reinstall(
                    oid(), body.get("result", "ok"),
                    body.get("note", ""), role))
            if seg == ["reminders" , "tick"]:
                fired = reminder_worker.tick() if reminder_worker \
                    else svc.fire_reminders()
                return self._send(200, {"fired": fired})
            return self._error(404, "接口不存在")

    return Handler


def create_server(svc, reminder_worker=None, photo_dir="data/photos",
                  host="127.0.0.1", port=8765):
    handler = make_handler(svc, reminder_worker, photo_dir)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(svc, reminder_worker=None, photo_dir="data/photos",
          host="127.0.0.1", port=8765):
    httpd = create_server(svc, reminder_worker, photo_dir, host, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
