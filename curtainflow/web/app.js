/* 帘务通前端：原生 JS 单页，七个产品视图 + 提醒铃铛 + 角色切换。 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = { tab: "orders", role: "crew", orderId: null, roles: [], meta: null };

const TABS = [
  { id: "orders", name: "上门拆卸" },
  { id: "tree", name: "窗位清单" },
  { id: "wash", name: "洗护队列" },
  { id: "repair", name: "修补赔付" },
  { id: "delivery", name: "配包配送" },
  { id: "reinstall", name: "复装对照" },
  { id: "fees", name: "费用复核" },
];

const ORDER_STATUS = {
  created: ["待预约", ""], scheduled: ["已预约", "blue"],
  dismantling: ["拆卸中", "warn"], in_care: ["洗护中", "blue"],
  packing: ["配包中", "blue"], delivering: ["配送中", "warn"],
  reinstalling: ["复装中", "warn"], review: ["待复核", "warn"],
  closed: ["已关闭", "ok"],
};
const PANEL_STATUS = {
  registered: ["待拆卸", ""], bagged: ["已封袋", ""], in_transit: ["运输中", "blue"],
  at_shop: ["待洗护", "warn"], washing: ["洗护中", "blue"], drying: ["烘干中", "blue"],
  ready: ["待配包", "blue"], packed: ["已配包", ""], delivering: ["配送中", "warn"],
  delivered: ["待复装", "warn"], reinstalled: ["待验收", "warn"], accepted: ["已验收", "ok"],
};
const BATCH_STATUS = { planned: ["待发车", "warn"], delivering: ["配送中", "blue"], delivered: ["已送达", "ok"] };
const PKG_STATUS = { packing: ["配包中", ""], packed: ["待发运", "blue"], delivering: ["配送中", "warn"], delivered: ["已送达", "ok"] };
const BAG_STATUS = { sealed: ["已封签", ""], in_transit: ["运输中", "blue"], arrived: ["已到厂", "ok"] };
const ROLE_LABEL = { crew: "拆装技师", washer: "洗护员", cs: "客服", cs_lead: "客服主管", finance: "财务" };

// ------------------------------------------------------------ 基础

async function api(method, path, body) {
  const opt = { method, headers: { "X-Role": state.role } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function badge(text, cls = "") { return `<span class="badge ${cls}">${esc(text)}</span>`; }
function statusBadge(map, key) {
  const [t, c] = map[key] || [key, ""];
  return badge(t, c);
}
function fmt(ts) {
  if (!ts) return "—";
  return ts.replace("T", " ").slice(0, 16);
}
function shortCode(code) {
  // 每片帘布只显示必要编号：ORD-0001-KT-01-P1 -> P1
  const m = /-P(\d+)$/.exec(code);
  return m ? `P${m[1]}` : code;
}
function money(n) {
  const v = Number(n || 0);
  const cls = v < 0 ? "amount-neg" : "amount-pos";
  return `<span class="${cls}">${v < 0 ? "-" : ""}¥${Math.abs(v).toFixed(2)}</span>`;
}

let toastTimer;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

function modal(title, bodyHtml, actions) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  const bar = $("#modal-actions");
  bar.innerHTML = "";
  (actions || [{ text: "关闭", cls: "ghost", onClick: closeModal }]).forEach(a => {
    const b = document.createElement("button");
    b.className = `btn ${a.cls || ""}`;
    b.textContent = a.text;
    b.onclick = () => {
      if (a.onClick === false) return;
      (a.onClick || closeModal)();
    };
    bar.appendChild(b);
  });
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); }
$("#modal").addEventListener("click", e => { if (e.target.id === "modal") closeModal(); });

function confirmModal(title, text, onOk, okCls = "") {
  modal(title, `<p>${esc(text)}</p>`, [
    { text: "取消", cls: "ghost" },
    { text: "确定", cls: okCls, onClick: () => { closeModal(); onOk(); } },
  ]);
}

// ------------------------------------------------------------ 外壳

async function init() {
  state.meta = await api("GET", "/api/meta");
  const sel = $("#role-select");
  sel.innerHTML = state.meta.roles.map(r =>
    `<option value="${r.id}">${esc(r.name)}</option>`).join("");
  sel.value = state.role;
  sel.onchange = () => { state.role = sel.value; render(); };
  $("#tabs").innerHTML = TABS.map(t =>
    `<button data-tab="${t.id}">${t.name}</button>`).join("");
  $$("#tabs button").forEach(b => b.onclick = () => {
    state.tab = b.dataset.tab; render();
  });
  $("#bell").onclick = toggleReminders;
  await render();
  setInterval(refreshReminders, 30000);
  refreshReminders();
}

async function render() {
  $$("#tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === state.tab));
  const main = $("#main");
  main.innerHTML = "";
  const fn = {
    orders: viewOrders, tree: viewTree, wash: viewWash,
    repair: viewRepair, delivery: viewDelivery,
    reinstall: viewReinstall, fees: viewFees,
  }[state.tab];
  try { await fn(main); }
  catch (e) { main.innerHTML = `<div class="card">加载失败：${esc(e.message)}</div>`; }
}

function orderPicker(orders, current, onchange) {
  return `<select id="order-picker">
    ${orders.map(o => `<option value="${o.id}" ${o.id === current ? "selected" : ""}>
      ${esc(o.code)} · ${esc(o.customer_name)} · ${esc(o.address_code)}</option>`).join("")}
  </select>`;
}

async function pickOrder(orders) {
  if (!orders.length) { toast("暂无订单"); return null; }
  if (!state.orderId || !orders.some(o => o.id === state.orderId))
    state.orderId = orders[0].id;
  return state.orderId;
}

// ------------------------------------------------------------ 视图一：上门拆卸

async function viewOrders(main) {
  const orders = await api("GET", "/api/orders");
  const counts = orders.reduce((m, o) => (m[o.status] = (m[o.status] || 0) + 1, m), {});
  main.innerHTML = `
    <div class="grid cols-4">
      <div class="stat"><div class="n">${orders.length}</div><div class="l">订单总数</div></div>
      <div class="stat"><div class="n">${counts.dismantling || 0}</div><div class="l">拆卸中</div></div>
      <div class="stat"><div class="n">${counts.reinstalling || 0}</div><div class="l">复装中</div></div>
      <div class="stat"><div class="n">${counts.closed || 0}</div><div class="l">已关闭</div></div>
    </div>
    <div class="card">
      <div class="card-head"><h2>订单与上门拆卸</h2>
        <span class="muted">当前角色：${ROLE_LABEL[state.role]} · 客户信息均为虚构</span></div>
      <table><thead><tr>
        <th>订单号</th><th>客户</th><th>住址代号</th><th>预约时间</th><th>帘片</th><th>状态</th><th>操作</th>
      </tr></thead><tbody>
      ${orders.map(o => `<tr>
        <td><a class="link" data-open="${o.id}">${esc(o.code)}</a></td>
        <td>${esc(o.customer_name)}</td>
        <td>${esc(o.address_code)}</td>
        <td>${fmt(o.appoint_at)}</td>
        <td>${o.panel_count}</td>
        <td>${statusBadge(ORDER_STATUS, o.status)}</td>
        <td class="row-actions">${orderActions(o)}</td>
      </tr>`).join("")}
      </tbody></table>
    </div>`;
  $$("[data-open]", main).forEach(a => a.onclick = () => {
    state.orderId = Number(a.dataset.open); state.tab = "tree"; render();
  });
  $$("[data-act]", main).forEach(b => b.onclick = () => orderAction(b.dataset, orders));
}

function orderActions(o) {
  const btns = [];
  if (["created", "scheduled"].includes(o.status))
    btns.push(`<button class="btn small" data-act="schedule" data-id="${o.id}">预约</button>`);
  if (["created", "scheduled"].includes(o.status))
    btns.push(`<button class="btn small" data-act="dismantle" data-id="${o.id}">开始拆卸</button>`);
  if (o.status === "dismantling")
    btns.push(`<button class="btn small" data-act="bags" data-id="${o.id}">拆卸装袋</button>`);
  if (["scheduled", "dismantling"].includes(o.status))
    btns.push(`<button class="btn small ghost" data-act="postpone" data-id="${o.id}">延期</button>`);
  btns.push(`<button class="btn small ghost" data-act="bags" data-id="${o.id}">运输袋</button>`);
  return btns.join(" ");
}

async function orderAction(d, orders) {
  const id = Number(d.id);
  if (d.act === "schedule") {
    modal("预约上门时间",
      `<label class="fld">时间<input type="datetime-local" id="m-time"></label>`, [
        { text: "取消", cls: "ghost" },
        { text: "保存预约", onClick: async () => {
          const v = $("#m-time").value;
          if (!v) return toast("请选择时间", "err");
          await api("POST", `/api/orders/${id}/schedule`,
            { appoint_at: v.replace("T", "T") + ":00" });
          closeModal(); toast("已预约", "ok"); render();
        } }]);
    return;
  }
  if (d.act === "dismantle") {
    await api("POST", `/api/orders/${id}/start-dismantle`, {});
    toast("已开始上门拆卸", "ok"); render(); return;
  }
  if (d.act === "postpone") {
    modal("客户延期登记",
      `<label class="fld">改期时间<input type="datetime-local" id="m-time"></label>
       <label class="fld">原因<input id="m-reason" placeholder="如：客户临时出差"></label>
       <p class="muted">延期会写入异常留痕（客服/客服主管）。</p>`, [
        { text: "取消", cls: "ghost" },
        { text: "确认延期", cls: "warn", onClick: async () => {
          const v = $("#m-time").value, reason = $("#m-reason").value;
          if (!v) return toast("请选择时间", "err");
          await api("POST", `/api/orders/${id}/postpone`,
            { appoint_at: v + ":00", reason });
          closeModal(); toast("延期已留痕", "ok"); render();
        } }]);
    return;
  }
  if (d.act === "bags") openBagModal(id);
}

async function openBagModal(orderId) {
  const [tree, bags] = await Promise.all([
    api("GET", `/api/orders/${orderId}`),
    api("GET", `/api/orders/${orderId}/bags`)]);
  const winHtml = tree.rooms.map(r =>
    `<div class="win-row"><div class="win-head">
       <span><strong>${esc(r.name)}</strong> · <span class="win-code">${esc(r.windows.map(w => w.code).join("、"))}</span></span>
     </div>
     ${r.windows.map(w => `
       <div style="margin-top:8px">
         <div class="muted">窗位 ${esc(w.code)}（勾选本窗位帘片，按窗位封签）</div>
         ${w.panels.map(p => `
           <label class="panel-line">
             <input type="checkbox" class="bag-check" data-panel="${p.id}"
               ${p.status === "registered" ? "" : "disabled"} checked>
             <span class="pc" title="${esc(p.code)}">${shortCode(p.code)}</span>
             ${p.hook_count}钩 · ${esc(p.fabric)}
             ${statusBadge(PANEL_STATUS, p.status)}
           </label>`).join("")}
         <div class="panel-actions">
           ${w.panels.some(p => p.status === "registered")
            ? `<button class="btn small" data-seal="${w.id}">清点挂钩并封签</button>`
            : `<span class="muted">本窗位已封签</span>`}
         </div>
       </div>`).join("")}
    </div>`).join("");
  modal(`拆卸装袋 · ${esc(tree.code)}`,
    `<p class="muted" style="margin-bottom:8px">按窗位分组编号，逐片清点挂钩数量，装袋后生成封签号；封签时自动留存拆前照片。</p>
     ${winHtml}
     <h3 style="margin-top:14px">运输袋</h3>
     <table><thead><tr><th>封签号</th><th>窗位</th><th>帘片</th><th>状态</th><th></th></tr></thead><tbody>
     ${bags.length ? bags.map(b => `<tr>
       <td><span class="seal">${esc(b.seal_no)}</span></td>
       <td>${esc(b.window_code)}</td><td>${b.panel_count} 片</td>
       <td>${statusBadge(BAG_STATUS, b.status)}</td>
       <td class="row-actions">
         ${b.status === "sealed" ? `<button class="btn small" data-ship="${b.id}">发出运输</button>` : ""}
         ${b.status === "in_transit" ? `<button class="btn small" data-rcv="${b.id}">到厂扫码</button>` : ""}
       </td></tr>`).join("")
       : `<tr><td colspan="5" class="muted">尚未封袋</td></tr>`}
     </tbody></table>`, []);
  $$("[data-seal]").forEach(btn => btn.onclick = async () => {
    const win = btn.closest(".win-row");
    const ids = $$(".bag-check:checked", win).map(c => Number(c.dataset.panel));
    if (!ids.length) return toast("至少选择一片帘布", "err");
    try {
      await api("POST", `/api/windows/${btn.dataset.seal}/seal`, { panel_ids: ids });
      toast("已装袋封签，拆前照片已留存", "ok"); closeModal(); openBagModal(orderId);
    } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-ship]").forEach(btn => btn.onclick = async () => {
    await api("POST", `/api/bags/${btn.dataset.ship}/ship`, {});
    toast("已发出运输", "ok"); closeModal(); openBagModal(orderId);
  });
  $$("[data-rcv]").forEach(btn => btn.onclick = async () => {
    await api("POST", `/api/bags/${btn.dataset.rcv}/receive`, {});
    toast("已到厂入洗护队列", "ok"); closeModal(); openBagModal(orderId);
  });
}

// ------------------------------------------------------------ 视图二：窗位清单（树形）

async function viewTree(main) {
  const orders = await api("GET", "/api/orders");
  const id = await pickOrder(orders);
  if (!id) return;
  const tree = await api("GET", `/api/orders/${id}`);
  const total = tree.rooms.reduce((n, r) =>
    n + r.windows.reduce((m, w) => m + w.panels.length, 0), 0);
  const accepted = tree.rooms.reduce((n, r) =>
    n + r.windows.reduce((m, w) => m + w.panels.filter(p => p.status === "accepted").length, 0), 0);
  main.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>${orderPicker(orders, id)}
          <h2 style="margin:8px 0 2px">${esc(tree.code)} · ${esc(tree.customer_name)}
            ${statusBadge(ORDER_STATUS, tree.status)}</h2>
          <div class="muted">住址代号 ${esc(tree.address_code)} · ${esc(tree.address)} · ${esc(tree.phone)} · 预约 ${fmt(tree.appoint_at)}</div>
        </div>
        <div class="kv" style="min-width:200px">
          <dt>帘片进度</dt><dd><strong>${accepted}</strong> / ${total} 片已验收</dd>
          <dt>窗位</dt><dd>${tree.rooms.reduce((n, r) => n + r.windows.length, 0)} 个</dd>
        </div>
      </div>
    </div>
    <div class="card">
      <h2>房间 / 窗位 / 帘片树形清单</h2>
      ${tree.rooms.map(r => `
        <details class="tree-room" open>
          <summary><span>${esc(r.name)}</span>
            <span class="muted">${r.windows.length} 个窗位</span></summary>
          <div class="tree-room-body">
          ${r.windows.map(w => `
            <div class="win-row ${w.accepted ? "accepted" : ""}">
              <div class="win-head">
                <span class="win-code">窗位 ${esc(w.code)}</span>
                ${w.accepted ? badge("本窗位已验收", "ok") : badge("未全部验收", "warn")}
              </div>
              <div>${w.panels.map(p => `
                <span class="panel-line" title="${esc(p.code)}｜${esc(p.fabric)}｜${esc(p.care_req || "常规洗护")}${p.wash_forbidden ? "｜禁洗" : ""}">
                  <span class="pc">${shortCode(p.code)}</span>
                  ${p.hook_count}钩
                  ${p.wash_forbidden ? badge("禁洗", "danger") : ""}
                  ${statusBadge(PANEL_STATUS, p.status)}
                </span>`).join("")}</div>
            </div>`).join("")}
          </div>
        </details>`).join("")}
    </div>
    <div class="card">
      <h2>异常留痕</h2>
      ${tree.incidents.length ? `<div class="timeline">${tree.incidents.map(i => `
        <div class="t-item ${i.status === "open" ? "bad" : "good"}">
          <span class="t-time">${fmt(i.created_at)}</span>
          <strong>${esc(i.type_label)}</strong>
          ${i.panel_code ? `· <span class="pc">${esc(i.panel_code)}</span>` : ""}
          ${esc(i.detail)}
          ${badge(i.status === "open" ? "待处理" : "已处理",
                   i.status === "open" ? "danger" : "ok")}
        </div>`).join("")}</div>`
        : `<p class="muted">暂无异常记录</p>`}
    </div>`;
  $("#order-picker").onchange = e => { state.orderId = Number(e.target.value); render(); };
}

// ------------------------------------------------------------ 视图三：洗护队列

async function viewWash(main) {
  const queue = await api("GET", "/api/wash-queue");
  main.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>洗护队列</h2>
        <span class="muted">到厂扫码入队 · 48 小时洗护时限 · 禁洗布料仅除尘挂晾</span></div>
      <table><thead><tr>
        <th>帘片</th><th>订单</th><th>布料</th><th>挂钩</th><th>洗护要求</th>
        <th>任务时限</th><th>状态</th><th>操作</th>
      </tr></thead><tbody>
      ${queue.length ? queue.map(t => {
        const overdue = t.due_at && t.due_at < new Date().toISOString() &&
          ["queued", "washing"].includes(t.status);
        return `<tr>
          <td><span class="pc">${esc(t.panel_code)}</span></td>
          <td>${esc(t.order_code)}</td>
          <td>${esc(t.fabric)}${t.wash_forbidden ? " " + badge("禁洗", "danger") : ""}</td>
          <td>${t.hook_count}</td>
          <td>${esc(t.care_req || "常规")}</td>
          <td>${fmt(t.due_at)} ${overdue ? badge("超时", "danger") : ""}</td>
          <td>${statusBadge(PANEL_STATUS, t.panel_status)}</td>
          <td class="row-actions">
            ${t.status === "queued" && !t.wash_forbidden
              ? `<button class="btn small" data-wash="${t.panel_id}">开始洗护</button>` : ""}
            ${["queued", "washing"].includes(t.status)
              ? `<button class="btn small ghost" data-dry="${t.panel_id}">${t.wash_forbidden ? "除尘挂晾完成" : "烘干完成"}</button>` : ""}
            <button class="btn small ghost" data-damage="${t.panel_id}">登记损伤</button>
            <button class="btn small ghost" data-repairp="${t.panel_id}">登记修补</button>
          </td></tr>`;
      }).join("") : `<tr><td colspan="8" class="muted">队列为空</td></tr>`}
      </tbody></table>
    </div>`;
  $$("[data-wash]").forEach(b => b.onclick = async () => {
    try { await api("POST", `/api/panels/${b.dataset.wash}/start-wash`, {});
      toast("已开始洗护", "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-dry]").forEach(b => b.onclick = async () => {
    await api("POST", `/api/panels/${b.dataset.dry}/dry`, {});
    toast("烘干完成，可按原窗位配包", "ok"); render();
  });
  $$("[data-damage]").forEach(b => b.onclick = () => damageModal(Number(b.dataset.damage)));
  $$("[data-repairp]").forEach(b => b.onclick = () => repairModal(Number(b.dataset.repairp)));
}

async function damageModal(panelId) {
  const types = state.meta.incident_types.filter(t =>
    ["shrink", "dye", "hook_missing", "uneven"].includes(t.id));
  modal("洗护人员登记损伤",
    `<label class="fld">损伤类型<select id="m-type">
       ${types.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}
     </select></label>
     <label class="fld">情况说明<textarea id="m-detail" rows="3" placeholder="如：烘干后纵向缩水约 3cm"></textarea></label>`,
    [{ text: "取消", cls: "ghost" },
     { text: "提交留痕", onClick: async () => {
       await api("POST", `/api/panels/${panelId}/damage`,
         { type: $("#m-type").value, detail: $("#m-type").selectedOptions[0].textContent + "：" + $("#m-detail").value });
       closeModal(); toast("损伤已登记，等待客服处理", "ok"); render();
     } }]);
}

async function repairModal(panelId) {
  modal("登记修补",
    `<label class="fld">帘片 ID<input id="m-pid" value="${panelId || ""}"></label>
     <label class="fld">修补说明<textarea id="m-desc" rows="3"></textarea></label>
     <p class="muted">同一帘片第 2 次修补自动记录“二次修补”留痕。</p>`,
    [{ text: "取消", cls: "ghost" },
     { text: "提交修补单", onClick: async () => {
       const pid = Number($("#m-pid").value);
       if (!pid) return toast("请填写帘片 ID", "err");
       await api("POST", `/api/panels/${pid}/repair`,
         { description: $("#m-desc").value });
       closeModal(); toast("修补单已创建", "ok"); render();
     } }]);
}

// ------------------------------------------------------------ 视图四：修补赔付

async function viewRepair(main) {
  const [incidents, claims, repairs] = await Promise.all([
    api("GET", "/api/incidents"),
    api("GET", "/api/claims"),
    api("GET", "/api/repairs")]);
  const isLead = state.role === "cs_lead";
  main.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>异常处理与赔付返工</h2>
        <span class="muted">赔付与返工须客服主管批准 · 当前：${ROLE_LABEL[state.role]}</span></div>
      <table><thead><tr>
        <th>时间</th><th>订单</th><th>帘片</th><th>类型</th><th>说明</th>
        <th>登记人</th><th>状态</th><th>操作</th>
      </tr></thead><tbody>
      ${incidents.map(i => `<tr>
        <td class="muted">${fmt(i.created_at)}</td>
        <td>${esc(i.order_code)}</td>
        <td><span class="pc">${esc(i.panel_code || "—")}</span></td>
        <td>${esc(i.type_label)}</td><td>${esc(i.detail)}</td>
        <td>${esc(ROLE_LABEL[i.created_by] || i.created_by)}</td>
        <td>${badge(i.status === "open" ? "待处理" : "已处理",
                    i.status === "open" ? "danger" : "ok")}</td>
        <td class="row-actions">
          ${i.status === "open" && i.panel_id && ["cs", "cs_lead"].includes(state.role)
            ? `<button class="btn small" data-claim="${i.id}">提交赔付</button>` : ""}
          ${i.status === "open" && i.panel_id
            ? `<button class="btn small ${isLead ? "warn" : "ghost"}" data-rework="${i.id}"
               ${isLead ? "" : "title='仅客服主管可批准'"}">批准返工</button>` : ""}
        </td></tr>`).join("")}
      </tbody></table>
    </div>
    <div class="card">
      <h2>赔付审批</h2>
      <table><thead><tr>
        <th>订单</th><th>帘片</th><th>异常</th><th>金额</th><th>事由</th>
        <th>申请人</th><th>审批人</th><th>状态</th><th></th>
      </tr></thead><tbody>
      ${claims.map(c => `<tr>
        <td>${esc(c.order_code)}</td>
        <td><span class="pc">${esc(c.panel_code || "—")}</span></td>
        <td>${esc(c.type_label)}</td>
        <td>${money(-c.amount)}</td><td>${esc(c.reason)}</td>
        <td>${esc(ROLE_LABEL[c.proposed_by] || c.proposed_by)}</td>
        <td>${esc(c.decided_by ? ROLE_LABEL[c.decided_by] || c.decided_by : "—")}</td>
        <td>${c.status === "pending" ? badge("待审批", "warn")
             : c.status === "approved" ? badge("已批准", "ok")
             : badge("已驳回", "danger")}</td>
        <td class="row-actions">
          ${c.status === "pending"
            ? `<button class="btn small ${isLead ? "" : "ghost"}" data-approve="${c.id}">批准</button>
               <button class="btn small danger" data-reject="${c.id}">驳回</button>
               ${!isLead ? `<span class="muted">仅主管可批</span>` : ""}` : ""}
        </td></tr>`).join("")}
      </tbody></table>
    </div>
    <div class="card">
      <div class="card-head"><h2>修补记录</h2>
        <button class="btn small ghost" id="new-repair">登记修补</button></div>
      <table><thead><tr>
        <th>订单</th><th>帘片</th><th>轮次</th><th>说明</th><th>状态</th><th>完成时间</th><th></th>
      </tr></thead><tbody>
      ${repairs.map(r => `<tr>
        <td>${esc(r.order_code)}</td>
        <td><span class="pc">${esc(r.panel_code)}</span></td>
        <td>第 ${r.round} 次${r.round >= 2 ? " " + badge("二次修补", "warn") : ""}</td>
        <td>${esc(r.description)}</td>
        <td>${r.status === "open" ? badge("修补中", "warn") : badge("已完成", "ok")}</td>
        <td>${fmt(r.done_at)}</td>
        <td>${r.status === "open"
          ? `<button class="btn small" data-finish-r="${r.id}">完成修补</button>` : ""}</td>
      </tr>`).join("")}
      </tbody></table>
    </div>`;
  $$("[data-claim]").forEach(b => b.onclick = () => claimModal(Number(b.dataset.claim)));
  $$("[data-rework]").forEach(b => b.onclick = () => {
    if (state.role !== "cs_lead") return toast("返工批准仅客服主管可操作", "err");
    confirmModal("批准返工", "批准后帘片退回洗护队列重新洗烘，并记录返工费用。", async () => {
      try { await api("POST", `/api/incidents/${b.dataset.rework}/rework`, {});
        toast("已批准返工，帘片退回洗护队列", "ok"); render();
      } catch (e) { toast(e.message, "err"); }
    }, "warn");
  });
  $$("[data-approve]").forEach(b => b.onclick = () => decideClaim(Number(b.dataset.approve), true));
  $$("[data-reject]").forEach(b => b.onclick = () => decideClaim(Number(b.dataset.reject), false));
  $$("[data-finish-r]").forEach(b => b.onclick = async () => {
    await api("POST", `/api/repairs/${b.dataset.finishR}/finish`, {});
    toast("修补完成", "ok"); render();
  });
  $("#new-repair").onclick = () => repairModal(null);
}

function claimModal(incidentId) {
  modal("提交赔付申请",
    `<label class="fld">赔付金额（元）<input id="m-amount" type="number" step="0.01" value="30"></label>
     <label class="fld">事由<input id="m-reason" placeholder="如：金属挂钩2个补发"></label>
     <p class="muted">提交后由客服主管审批，批准后自动计入费用复核（负数抵扣）。</p>`,
    [{ text: "取消", cls: "ghost" },
     { text: "提交审批", onClick: async () => {
       const amount = Number($("#m-amount").value);
       if (!(amount > 0)) return toast("金额需大于 0", "err");
       await api("POST", `/api/incidents/${incidentId}/claim`,
         { amount, reason: $("#m-reason").value });
       closeModal(); toast("赔付申请已提交，等待主管审批", "ok"); render();
     } }]);
}

async function decideClaim(id, approve) {
  if (state.role !== "cs_lead") return toast("赔付审批仅客服主管可操作", "err");
  try {
    await api("POST", `/api/claims/${id}/decide`, { approve });
    toast(approve ? "赔付已批准" : "已驳回", approve ? "ok" : ""); render();
  } catch (e) { toast(e.message, "err"); }
}

// ------------------------------------------------------------ 视图五：配包配送

async function viewDelivery(main) {
  const [orders, packages, batches] = await Promise.all([
    api("GET", "/api/orders"),
    api("GET", "/api/packages"),
    api("GET", "/api/batches")]);
  // 可配包的窗位：该订单有 ready 帘片
  const readyByOrder = {};
  for (const o of orders) {
    if (!["in_care", "packing", "delivering", "reinstalling"].includes(o.status)) continue;
    const tree = await api("GET", `/api/orders/${o.id}`);
    const wins = [];
    for (const r of tree.rooms) for (const w of r.windows) {
      const ready = w.panels.filter(p => p.status === "ready");
      if (ready.length) wins.push({ room: r.name, win: w, ready });
    }
    if (wins.length) readyByOrder[o.id] = { order: o, wins };
  }
  const packed = packages.filter(p => p.status === "packed");
  main.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>按原窗位配包</h2>
        <span class="muted">洗净烘干后仍按原窗位配包 · 同一帘片不能归入两个配送包</span></div>
      ${Object.values(readyByOrder).length ? Object.values(readyByOrder).map(x => `
        <h3>${esc(x.order.code)} · ${esc(x.order.customer_name)}</h3>
        ${x.wins.map(w => `
          <div class="win-row">
            <div class="win-head">
              <span><strong>${esc(w.room)}</strong> · <span class="win-code">${esc(w.win.code)}</span>
                待配 ${w.ready.length} 片：
                ${w.ready.map(p => `<span class="panel-line"><span class="pc" title="${esc(p.code)}">${shortCode(p.code)}</span>${p.hook_count}钩</span>`).join("")}
              </span>
              <button class="btn small" data-pack="${w.win.id}"
                data-panels='${JSON.stringify(w.ready.map(p => p.id))}'>按此窗位配包</button>
            </div>
          </div>`).join("")}`).join("")
        : `<p class="muted">暂无可配包的就绪帘片</p>`}
    </div>
    <div class="card">
      <div class="card-head"><h2>配送包</h2>
        ${packed.length ? `<button class="btn small" id="new-batch">创建配送批次（选中 ${packed.length} 个待发运包）</button>` : ""}
      </div>
      <table><thead><tr>
        <th>封签号</th><th>订单</th><th>窗位</th><th>帘片</th><th>批次</th><th>状态</th>
      </tr></thead><tbody>
      ${packages.map(p => `<tr>
        <td><span class="seal pkg">${esc(p.seal_no)}</span></td>
        <td>${esc(p.order_code)}</td><td>${esc(p.window_code)}</td>
        <td title="${esc(p.panel_codes)}">${p.panel_count} 片</td>
        <td>${esc(p.batch_code || "—")}</td>
        <td>${statusBadge(PKG_STATUS, p.status)}</td>
      </tr>`).join("")}
      </tbody></table>
    </div>
    <div class="card">
      <h2>配送批次（模拟配送适配器）</h2>
      <table><thead><tr>
        <th>批次号</th><th>车辆</th><th>包数</th><th>创建</th><th>送达</th><th>状态</th><th>操作</th>
      </tr></thead><tbody>
      ${batches.length ? batches.map(b => `<tr>
        <td><strong>${esc(b.code)}</strong></td>
        <td>${esc(b.vehicle)}</td><td>${b.packages.length}</td>
        <td>${fmt(b.created_at)}</td><td>${fmt(b.delivered_at)}</td>
        <td>${statusBadge(BATCH_STATUS, b.status)}</td>
        <td class="row-actions">
          ${b.status === "planned" ? `<button class="btn small" data-dispatch="${b.id}">发车</button>` : ""}
          ${b.status === "delivering" ? `<button class="btn small ghost" data-deliver="${b.id}">确认送达</button>` : ""}
        </td></tr>`).join("")
        : `<tr><td colspan="7" class="muted">暂无批次</td></tr>`}
      </tbody></table>
    </div>`;
  $$("[data-pack]").forEach(btn => btn.onclick = async () => {
    try {
      await api("POST", `/api/windows/${btn.dataset.pack}/pack`,
        { panel_ids: JSON.parse(btn.dataset.panels) });
      toast("已按原窗位配包并生成封签号", "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  });
  const nb = $("#new-batch");
  if (nb) nb.onclick = async () => {
    await api("POST", "/api/batches", { package_ids: packed.map(p => p.id) });
    toast("配送批次已创建", "ok"); render();
  };
  $$("[data-dispatch]").forEach(b => b.onclick = async () => {
    await api("POST", `/api/batches/${b.dataset.dispatch}/dispatch`, {});
    toast("已发车（配送适配器已建运单）", "ok"); render();
  });
  $$("[data-deliver]").forEach(b => b.onclick = async () => {
    await api("POST", `/api/batches/${b.dataset.deliver}/deliver`, {});
    toast("客户已签收，可入户复装", "ok"); render();
  });
}

// ------------------------------------------------------------ 视图六：复装对照

async function viewReinstall(main) {
  const orders = await api("GET", "/api/orders");
  const live = orders.filter(o => ["delivering", "reinstalling"].includes(o.status));
  const id = await pickOrder(live.length ? live : orders);
  if (!id) return;
  const [tree, packages, records] = await Promise.all([
    api("GET", `/api/orders/${id}`),
    api("GET", `/api/orders/${id}/packages`),
    api("GET", `/api/orders/${id}/reinstalls`)]);
  const deliveredPkgs = packages.filter(p => p.status === "delivered");
  main.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>${orderPicker(live.length ? live : orders, id)}
          <h2 style="margin:8px 0 0">复装对照 · ${esc(tree.code)} · ${esc(tree.customer_name)}</h2></div>
        ${badge("逐片复装 · 对照拆前照片 · 全窗位验收后关单", "blue")}
      </div>
      <div class="filter-bar">
        <input id="scan-panel" placeholder="扫描/输入帘片编号，如 ${tree.rooms[0] ? "P1" : ""}">
        <select id="scan-pkg">
          ${deliveredPkgs.map(p => `<option value="${p.id}">${esc(p.seal_no)} · 窗位 ${esc(p.window_code)}</option>`).join("")}
        </select>
        <button class="btn" id="scan-btn">复装扫描</button>
        <span class="muted">扫描校验帘片归属配送包与原窗位，串包自动留痕。</span>
      </div>
    </div>
    ${tree.rooms.map(r => r.windows.map(w => {
      const pkg = deliveredPkgs.find(p => p.window_id === w.id);
      return `
      <div class="card">
        <div class="card-head">
          <h3>${esc(r.name)} · 窗位 <span class="win-code">${esc(w.code)}</span>
            ${w.accepted ? badge("本窗位验收完成", "ok") : badge("待验收", "warn")}</h3>
          ${pkg ? `<span class="seal pkg">${esc(pkg.seal_no)}</span>` : `<span class="muted">配送包未送达</span>`}
        </div>
        <div class="grid" style="grid-template-columns:repeat(auto-fill,minmax(240px,1fr))">
        ${w.panels.map(p => panelReinstallCard(p, pkg, records)).join("")}
        </div>
      </div>`;
    }).join("")).join("")}
    <div class="card">
      <h2>复装记录</h2>
      <table><thead><tr>
        <th>帘片</th><th>配送包封签号</th><th>扫描时间</th><th>验收时间</th><th>结果</th>
      </tr></thead><tbody>
      ${records.length ? records.map(r => `<tr>
        <td><span class="pc">${esc(r.panel_code)}</span></td>
        <td><span class="seal pkg" style="padding:2px 8px;font-size:12px">${esc(r.seal_no)}</span></td>
        <td>${fmt(r.scanned_at)}</td><td>${fmt(r.accepted_at)}</td>
        <td>${r.accepted ? badge("验收通过", "ok")
             : r.result === "uneven" ? badge("复装不平整·待调整", "danger")
             : badge("待验收", "warn")}
          ${r.note ? `<span class="muted"> ${esc(r.note)}</span>` : ""}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="muted">暂无复装记录</td></tr>`}
      </tbody></table>
    </div>`;
  $("#order-picker").onchange = e => { state.orderId = Number(e.target.value); render(); };
  $("#scan-btn").onclick = async () => {
    const code = $("#scan-panel").value.trim().toUpperCase();
    const pkgId = Number($("#scan-pkg").value);
    if (!code) return toast("请输入帘片编号", "err");
    // 支持只输入 P1 这样的必要编号
    let panel;
    if (/^P\d+$/.test(code)) {
      panel = tree.rooms.flatMap(r => r.windows.flatMap(w => w.panels))
        .find(p => shortCode(p.code) === code);
    } else {
      panel = tree.rooms.flatMap(r => r.windows.flatMap(w => w.panels))
        .find(p => p.code === code || p.code.endsWith("-" + code));
    }
    if (!panel) return toast("该订单中找不到此帘片编号", "err");
    try {
      await api("POST", `/api/packages/${pkgId}/scan`, { panel_id: panel.id });
      toast(`扫描成功：${shortCode(panel.code)} 可复装`, "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  };
  $$("[data-accept]").forEach(b => b.onclick = () => {
    const id = Number(b.dataset.accept);
    modal("复装验收",
      `<label class="fld">备注<input id="m-note" placeholder="如：平整，挂钩齐全"></label>
       <p class="muted">验收通过后自动留存复装后照片，与拆前照片双图对照存档。</p>`,
      [{ text: "取消", cls: "ghost" },
       { text: "验收通过", onClick: async () => {
         try {
           await api("POST", `/api/panels/${id}/accept`,
             { result: "ok", note: $("#m-note").value });
           closeModal(); toast("验收通过", "ok"); render();
         } catch (e) { toast(e.message, "err"); }
       } }]);
  });
  $$("[data-uneven]").forEach(b => b.onclick = () => {
    const id = Number(b.dataset.uneven);
    modal("复装不平整登记",
      `<label class="fld">情况说明<textarea id="m-note" rows="3" placeholder="如：右侧下垂 1cm，需重新调挂钩"></textarea></label>`,
      [{ text: "取消", cls: "ghost" },
       { text: "提交并返工调整", cls: "warn", onClick: async () => {
         try {
           await api("POST", `/api/panels/${id}/accept`,
             { result: "uneven", note: $("#m-note").value });
           closeModal(); toast("已留痕，帘片退回待复装", "ok"); render();
         } catch (e) { toast(e.message, "err"); }
       } }]);
  });
}

function panelReinstallCard(p, pkg, records) {
  const rec = records.find(r => r.panel_id === p.id);
  const before = p.photo_before || "";
  const after = p.photo_after || "";
  const placeholder = (label) =>
    `<div style="aspect-ratio:3/4;display:flex;align-items:center;justify-content:center;color:#B6B0A4;font-size:13px;background:var(--linen-deep)">${label}</div>`;
  const canScan = pkg && p.status === "delivered";
  const scanned = ["reinstalled", "accepted"].includes(p.status);
  return `
    <div>
      <div class="compare" style="grid-template-columns:1fr 1fr">
        <div class="photo-box">
          ${before ? `<img src="${before}" alt="拆前">` : placeholder("暂无拆前照片")}
          <div class="cap before">拆前 · <span class="pc">${esc(p.code)}</span></div>
        </div>
        <div class="photo-box">
          ${p.status === "accepted" && after ? `<img src="${after}" alt="复装后">`
            : scanned ? placeholder("复装后照片待验收留存")
            : placeholder("尚未复装")}
          <div class="cap after">复装后</div>
        </div>
      </div>
      <div class="hook-line">
        <span>挂钩数量（照片下方清点）</span>
        <strong>${p.hook_count} 个</strong>
      </div>
      <div class="panel-actions">
        ${statusBadge(PANEL_STATUS, p.status)}
        ${rec && rec.result === "uneven" ? badge("不平整已留痕", "danger") : ""}
        ${canScan ? `<span class="muted">请先用上方扫描框扫描</span>` : ""}
        ${p.status === "reinstalled"
          ? `<button class="btn small" data-accept="${p.id}">验收通过</button>
             <button class="btn small warn" data-uneven="${p.id}">复装不平整</button>` : ""}
        ${p.status === "accepted" ? badge("已完成", "ok") : ""}
      </div>
    </div>`;
}

// ------------------------------------------------------------ 视图七：费用复核

async function viewFees(main) {
  const orders = await api("GET", "/api/orders");
  const id = await pickOrder(orders);
  if (!id) return;
  const r = await api("GET", `/api/orders/${id}/fees`);
  const o = r.order;
  main.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div>${orderPicker(orders, id)}
          <h2 style="margin:8px 0 0">费用复核 · ${esc(o.code)} · ${esc(ORDER_STATUS[o.status][0])}</h2></div>
        <div class="row-actions">
          <button class="btn" id="close-order">关闭订单</button>
          <button class="btn ghost" id="pay-btn">发起收款（模拟支付）</button>
        </div>
      </div>
      <p class="muted">关闭条件：所有窗位帘片验收通过且无未处理异常。赔付由客服主管批准后自动入账。</p>
    </div>
    <div class="grid cols-4">
      <div class="stat"><div class="n">${r.panel_count}</div><div class="l">帘片数</div></div>
      <div class="stat"><div class="n">${money(r.base_amount)}</div><div class="l">基础洗护费</div></div>
      <div class="stat"><div class="n">${money(r.extra_total)}</div><div class="l">修补/返工/赔付</div></div>
      <div class="stat"><div class="n">${money(r.total)}</div><div class="l">应收合计</div></div>
    </div>
    <div class="card">
      <h2>费用明细</h2>
      <table><thead><tr><th>项目</th><th>类型</th><th>金额</th><th>状态</th><th>时间</th></tr></thead>
      <tbody>
        <tr><td>拆洗护服务费（${r.panel_count} 片 × ¥68 + 挂钩 × ¥1.5）</td>
          <td>基础费</td><td>${money(r.base_amount)}</td><td>—</td><td>—</td></tr>
        ${r.extra_items.map(e => `<tr>
          <td>${esc(e.label)}</td><td>${esc(e.kind)}</td>
          <td>${money(e.amount)}</td>
          <td>${badge(e.status === "pending" ? "待核" : e.status, "blue")}</td>
          <td>${fmt(e.created_at)}</td></tr>`).join("")}
      </tbody></table>
      <div style="margin-top:12px" class="kv">
        <dt>应收</dt><dd>${money(r.total)}</dd>
        <dt>已收</dt><dd>${money(r.paid)}</dd>
        <dt>余额</dt><dd><strong>${money(r.balance)}</strong></dd>
      </div>
    </div>
    <div class="card">
      <h2>收款记录（付款模拟适配器）</h2>
      <table><thead><tr><th>时间</th><th>渠道</th><th>金额</th><th>交易号</th><th>状态</th></tr></thead>
      <tbody>${r.payments.length ? r.payments.map(p => `<tr>
        <td>${fmt(p.created_at)}</td><td>${esc(p.channel)}</td>
        <td>${money(p.amount)}</td><td><span class="pc">${esc(p.txn_no || "—")}</span></td>
        <td>${p.status === "success" ? badge("成功", "ok") : badge("失败", "danger")}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="muted">暂无收款</td></tr>`}</tbody></table>
    </div>`;
  $("#order-picker").onchange = e => { state.orderId = Number(e.target.value); render(); };
  $("#close-order").onclick = async () => {
    try {
      await api("POST", `/api/orders/${id}/close`, {});
      toast("订单已关闭", "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  };
  $("#pay-btn").onclick = async () => {
    try {
      const res = await api("POST", `/api/orders/${id}/pay`, {});
      toast(`收款成功：${res.payment.txn_no}`, "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  };
}

// ------------------------------------------------------------ 提醒

async function refreshReminders() {
  try {
    const data = await api("GET", "/api/reminders");
    const n = data.live.length;
    $("#bell-count").classList.toggle("hidden", n === 0);
    $("#bell-count").textContent = n;
    if (!$("#reminder-panel").classList.contains("hidden")) renderReminders(data);
  } catch (e) { /* 静默 */ }
}

function renderReminders(data) {
  const live = data.live.length ? data.live.map(i =>
    `<div class="reminder-item ${i.level === "info" ? "info" : ""}">
       <span class="rk">${i.level === "warn" ? "⚠" : "⏰"}</span>${esc(i.message)}</div>`).join("")
    : `<div class="muted">当前无到期提醒</div>`;
  const fired = data.fired.length ? `<h3 style="margin-top:10px">已触发记录</h3>` +
    data.fired.slice(0, 20).map(i =>
      `<div class="reminder-item ${i.level === "info" ? "info" : ""}">
        <span class="t-time">${fmt(i.fired_at)}</span>${esc(i.message)}</div>`).join("")
    : "";
  $("#reminder-panel").innerHTML = live + fired;
}

async function toggleReminders() {
  const panel = $("#reminder-panel");
  if (panel.classList.contains("hidden")) {
    const data = await api("GET", "/api/reminders");
    renderReminders(data);
    panel.classList.remove("hidden");
  } else panel.classList.add("hidden");
}

init();
