/* 帘净到家 —— 前端逻辑（无依赖，原生 JS） */
"use strict";

let STATE = null;
let ROLE = localStorage.getItem("cc-role") || "staff";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const wcode = (wid) => "W" + String(wid).padStart(4, "0");
const money = (n) => "¥" + Number(n).toFixed(2);

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = isErr ? "err" : "";
  t.style.display = "block";
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.style.display = "none"), isErr ? 4200 : 2200);
}

async function api(path, body = {}, method = "POST") {
  const opts = { method, headers: {} };
  if (method === "POST") {
    body.actor = ROLE;
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

async function refresh() {
  STATE = await (await fetch("/api/state")).json();
  renderAll();
  renderReminderBadge();
}

function L() { return STATE.labels; }
function pieceTag(p) {
  return `<span class="tag st-${p.status}">${L().piece[p.status] || p.status}</span>`;
}
function winTag(w) {
  return `<span class="tag st-${w.status}">${L().window[w.status] || w.status}</span>`;
}
function orderTag(o) {
  return `<span class="tag st-${o.status}">${L().order[o.status] || o.status}</span>`;
}
function orderRooms(oid) { return STATE.rooms[oid] || []; }
function roomWindows(rid) {
  return Object.values(STATE.windows).filter((w) => w.room_id === rid);
}
function winPieces(wid) { return STATE.pieces[wid] || []; }
function incidentsOf(oid) { return STATE.incidents.filter((i) => i.order_id === oid); }
function bagOfWindow(wid) { return STATE.bags.find((b) => b.window_id === wid); }
function pkgOfWindow(wid) { return STATE.packages.find((p) => p.window_id === wid); }

/* ================= 视图一：订单总览（树形清单） ================= */
function renderOrders() {
  const el = $("#view-orders");
  const cards = STATE.orders.map((o) => {
    const rooms = orderRooms(o.id).map((r) => {
      const wins = roomWindows(r.id).map((w) => {
        const chips = winPieces(w.id).map((p) =>
          `<span class="piece-chip" title="${esc(p.fabric)}｜挂钩 ${p.hooks_expected}｜${esc(p.wash_req || "常规洗护")}">
             <span class="code">${esc(p.code)}</span>${pieceTag(p)}
           </span>`).join("");
        const bag = bagOfWindow(w.id);
        return `<div class="tree-window">
          <div class="win-head">
            <span class="win-label">${wcode(w.id)} · ${esc(w.label)}</span>${winTag(w)}
            ${bag ? `<span class="seal small">${esc(bag.seal_no)}</span>` : ""}
          </div>
          <div class="pieces">${chips || '<span class="muted">尚无帘片</span>'}</div>
        </div>`;
      }).join("");
      return `<div class="tree-room"><div class="room-name">▸ ${esc(r.name)}</div>${wins}</div>`;
    }).join("");

    const incs = incidentsOf(o.id);
    const openInc = incs.filter((i) => !i.resolved).length;
    const acts = [];
    if (o.status === "created") acts.push(actBtn(`开始上门拆卸`, `doStartRemoval(${o.id})`));
    if (o.status === "removing") acts.push(actBtn(`封袋发运`, `doTransport(${o.id})`));
    if (["removing", "in_service"].includes(o.status) &&
        STATE.bags.some((b) => b.order_id === o.id && b.status === "in_transit"))
      acts.push(actBtn(`到厂接收`, `doReceive(${o.id})`));
    if (o.status !== "completed") {
      acts.push(actBtn(`客户延期`, `doPostpone(${o.id})`, "ghost"));
      acts.push(actBtn(`关闭订单`, `doClose(${o.id})`, "ok"));
    }
    return `<div class="card tree-order">
      <h3>${esc(o.code)} ${orderTag(o)}
        <span class="meta">${esc(o.customer_name)}｜${esc(o.phone)}｜住址代号 ${esc(o.address_alias)}</span>
      </h3>
      <div class="muted">预约复装：${o.appointment_at ? esc(o.appointment_at.replace("T", " ").slice(0, 16)) : "未定"}
        ${o.postponed ? `｜已延期 ${o.postponed} 次` : ""}
        ${openInc ? `｜<span style="color:var(--warn)">未处理异常 ${openInc} 条</span>` : ""}
        ${o.note ? `｜备注：${esc(o.note)}` : ""}</div>
      ${rooms}
      <div class="mt">${acts.join("")}</div>
    </div>`;
  }).join("");

  el.innerHTML = `
    <div class="card">
      <h3>新建订单</h3>
      <div class="row">
        <input id="no-name" placeholder="客户姓名" size="8">
        <input id="no-phone" placeholder="电话" size="12">
        <input id="no-alias" placeholder="住址代号 如 湖畔雅居3-2-501" size="20">
        <input id="no-addr" placeholder="详细地址" size="24">
        <input id="no-appt" type="datetime-local">
        <button class="act" onclick="doCreateOrder()">建单</button>
      </div>
      <div class="muted mt">建单后在「上门拆卸」页添加房间、窗位与帘片。</div>
    </div>
    ${cards || '<div class="empty">暂无订单</div>'}`;
}

/* ================= 视图二：上门拆卸 ================= */
function renderRemoval() {
  const el = $("#view-removal");
  const active = STATE.orders.filter((o) => ["created", "removing"].includes(o.status));
  el.innerHTML = `<div class="section-title">上门拆卸 —— 按窗位分组编号、清点挂钩、装袋封签</div>` +
    (active.map((o) => {
      const rooms = orderRooms(o.id).map((r) => {
        const wins = roomWindows(r.id).map((w) => {
          const bag = bagOfWindow(w.id);
          const rows = winPieces(w.id).map((p) => `
            <tr>
              <td><span class="piece-chip"><span class="code">${esc(p.code)}</span></span></td>
              <td>${esc(p.fabric)}${p.washable ? "" : ' <span class="tag st-banned">禁洗</span>'}</td>
              <td>${p.hooks_expected}</td>
              <td>${p.hooks_counted ?? "—"}</td>
              <td>${p.photo_before ? `<a href="${p.photo_before}" target="_blank">已拍</a>` : "未拍"}</td>
              <td>${pieceTag(p)}</td>
              <td>
                <input type="file" id="ph-${p.id}" accept="image/*" style="width:150px">
                <button class="act ghost" onclick="doUpload(${p.id},'before')">传拆前照</button>
                <button class="act" onclick="doCountHooks(${p.id},${p.hooks_expected})">清点挂钩</button>
              </td>
            </tr>`).join("");
          return `<div class="tree-window">
            <div class="win-head">
              <span class="win-label">${wcode(w.id)} · ${esc(w.label)}</span>${winTag(w)}
              ${bag
                ? `<span class="seal small">${esc(bag.seal_no)}</span><span class="muted">${L().window[w.status]}</span>`
                : `<button class="act" onclick="doSealBag(${w.id})">装袋封签</button>`}
              <button class="act ghost" onclick="doAddPiece(${w.id})">+ 登记帘片</button>
            </div>
            ${rows ? `<table class="grid mt"><thead><tr>
              <th>帘片编号</th><th>布料</th><th>应有挂钩</th><th>清点</th><th>拆前照</th><th>状态</th><th>操作</th>
            </tr></thead><tbody>${rows}</tbody></table>` : '<div class="muted mt">尚未登记帘片</div>'}
          </div>`;
        }).join("");
        return `<div class="tree-room">
          <div class="room-name">▸ ${esc(r.name)}
            <button class="act ghost" onclick="doAddWindow(${r.id})">+ 窗位</button></div>${wins}
        </div>`;
      }).join("");
      return `<div class="card tree-order">
        <h3>${esc(o.code)} ${orderTag(o)}
          <span class="meta">${esc(o.customer_name)}｜${esc(o.address_alias)}</span></h3>
        ${rooms}
        <div class="mt">
          <button class="act ghost" onclick="doAddRoom(${o.id})">+ 房间</button>
          ${o.status === "created" ? actBtn("开始上门拆卸", `doStartRemoval(${o.id})`) : ""}
          ${o.status === "removing" ? actBtn("全部封袋发运", `doTransport(${o.id})`) : ""}
        </div>
      </div>`;
    }).join("") || '<div class="empty">当前没有待拆卸的订单</div>');
}

/* ================= 视图三：窗位清单 ================= */
function renderWindows() {
  const el = $("#view-windows");
  const rows = [];
  for (const o of STATE.orders) {
    for (const r of orderRooms(o.id)) {
      for (const w of roomWindows(r.id)) {
        const bag = bagOfWindow(w.id);
        const pkg = pkgOfWindow(w.id);
        const pcs = winPieces(w.id);
        rows.push(`<tr>
          <td>${esc(o.code)}</td><td>${esc(r.name)}</td>
          <td><b>${wcode(w.id)}</b> ${esc(w.label)}</td>
          <td>${pcs.length} 片</td>
          <td>${pcs.map((p) => `<span class="piece-chip"><span class="code">${esc(p.code)}</span></span>`).join(" ")}</td>
          <td>${bag ? `<span class="seal small">${esc(bag.seal_no)}</span>` : "—"}</td>
          <td>${pkg ? `<span class="seal small">${esc(pkg.seal_no)}</span>` : "—"}</td>
          <td>${winTag(w)}</td>
        </tr>`);
      }
    }
  }
  el.innerHTML = `<div class="section-title">窗位清单 —— 每片帘布仅列必要编号，袋/包以封签号识别</div>
    <div class="card"><table class="grid"><thead><tr>
      <th>订单</th><th>房间</th><th>窗位</th><th>帘数</th><th>帘片编号</th><th>袋封签</th><th>包封签</th><th>状态</th>
    </tr></thead><tbody>${rows.join("") || '<tr><td colspan="8" class="empty">暂无窗位</td></tr>'}</tbody></table></div>`;
}

/* ================= 视图四：洗护队列 ================= */
const WASH_ACTIONS = {
  at_plant: (p) => `
    <button class="act" onclick="doWash(${p.id})">入洗</button>
    <button class="act ghost" onclick="doSkipWash(${p.id})">隔洗(禁洗)</button>
    <button class="act warn" onclick="doMisbag(${p.id})">串包登记</button>`,
  in_wash: (p) => `<button class="act" onclick="doWashFinish(${p.id})">洗护完成</button>`,
  washed: (p) => `
    <button class="act" onclick="doDry(${p.id})">入烘干</button>
    <button class="act ghost" onclick="doRepair(${p.id})">开修补</button>`,
  drying: (p) => `<button class="act" onclick="doDryFinish(${p.id})">烘干完成</button>`,
  dry: (p) => `
    <input type="file" id="ph-${p.id}" accept="image/*" style="width:140px">
    <button class="act ghost" onclick="doUpload(${p.id},'after_wash')">传洗后照</button>
    <button class="act" onclick="doReady(${p.id})">完工待配包</button>
    <button class="act ghost" onclick="doRepair(${p.id})">开修补</button>`,
  in_repair: (p) => `<span class="muted">修补中（见修补记录）</span>`,
  ready: (p) => `<button class="act ghost" onclick="doRepair(${p.id})">开修补</button>`,
};
function renderWash() {
  const el = $("#view-wash");
  const stages = ["at_plant", "in_wash", "washed", "drying", "dry", "in_repair", "ready"];
  const all = [];
  for (const [wid, pcs] of Object.entries(STATE.pieces))
    for (const p of pcs) if (stages.includes(p.status)) all.push(p);
  all.sort((a, b) => stages.indexOf(a.status) - stages.indexOf(b.status) || a.code.localeCompare(b.code));
  const rows = all.map((p) => `<tr>
    <td><span class="piece-chip"><span class="code">${esc(p.code)}</span></span></td>
    <td>${esc(p.fabric)}${p.washable ? "" : ' <span class="tag st-banned">禁洗</span>'}</td>
    <td class="muted">${esc(p.wash_req || "常规")}</td>
    <td>${pieceTag(p)}</td>
    <td>
      ${(WASH_ACTIONS[p.status] || (() => ""))(p)}
      <button class="act ghost" onclick="doIncident(${p.id})">异常留痕</button>
      <button class="act ghost" onclick="doDamage(${p.id})">登记损伤</button>
    </td></tr>`).join("");
  el.innerHTML = `<div class="section-title">洗护队列 —— 到厂 → 洗护 → 烘干 → 待配包</div>
    <div class="card"><table class="grid"><thead><tr>
      <th>帘片编号</th><th>布料</th><th>洗护要求</th><th>状态</th><th>操作</th>
    </tr></thead><tbody>${rows || '<tr><td colspan="5" class="empty">洗护队列为空</td></tr>'}</tbody></table></div>`;
}

/* ================= 视图五：修补记录（含损伤审批与异常留痕） ================= */
function renderRepair() {
  const el = $("#view-repair");
  const dmgRows = STATE.damages.map((d) => `<tr>
    <td>#${d.id}</td><td><span class="code">${esc(d.piece_code)}</span></td>
    <td>${esc(d.description)}</td><td class="muted">${esc(d.registered_by)}｜${esc(d.at.slice(0, 16).replace("T", " "))}</td>
    <td>${d.status === "pending" ? '<span class="tag st-in_wash">待审批</span>'
      : d.status === "compensated" ? '<span class="tag st-accepted">已赔付</span>'
      : d.status === "rework" ? '<span class="tag st-in_repair">已返工</span>'
      : '<span class="tag">已驳回</span>'}</td>
    <td>${d.status === "pending" ? `
      <button class="act ok" onclick="doDecide(${d.id},'compensate')">批准赔付</button>
      <button class="act" onclick="doDecide(${d.id},'rework')">批准返工</button>
      <button class="act ghost" onclick="doDecide(${d.id},'reject')">驳回</button>` : ""}</td>
  </tr>`).join("");

  const repRows = STATE.repairs.map((r) => `<tr>
    <td>#${r.id}</td><td><span class="code">${esc(r.piece_code)}</span></td>
    <td>第 ${r.round} 轮${r.round >= 2 ? ' <span class="tag st-banned">二次修补</span>' : ""}</td>
    <td>${esc(r.note)}</td>
    <td>${r.status === "open" ? '<span class="tag st-in_repair">修补中</span>' : '<span class="tag st-accepted">已完工</span>'}</td>
    <td>${r.status === "open" ? `<button class="act" onclick="doFinishRepair(${r.id})">完工</button>` : esc((r.finished_at || "").slice(0, 16).replace("T", " "))}</td>
  </tr>`).join("");

  const incRows = STATE.incidents.slice().reverse().map((i) => `
    <div class="incident ${i.resolved ? "resolved" : ""}">
      <span class="t">${L().incident[i.type] || i.type}</span>
      ${i.piece_code ? `<span class="code"> ${esc(i.piece_code)}</span>` : ""}
      ｜${esc(i.note)} <span class="muted">｜${esc(i.actor)}｜${esc(i.at.slice(0, 16).replace("T", " "))}</span>
      ${i.resolved
        ? `<span class="muted">｜已处理：${esc(i.resolved_note || "—")}</span>`
        : `<button class="act ghost" onclick="doResolve(${i.id})">销项</button>`}
    </div>`).join("");

  el.innerHTML = `
    <div class="section-title">损伤审批 —— 洗护人员登记，客服主管批准赔付 / 返工</div>
    <div class="card"><table class="grid"><thead><tr>
      <th>#</th><th>帘片</th><th>损伤描述</th><th>登记</th><th>状态</th><th>审批（仅客服主管）</th>
    </tr></thead><tbody>${dmgRows || '<tr><td colspan="6" class="empty">无损伤记录</td></tr>'}</tbody></table></div>
    <div class="section-title mt">修补记录</div>
    <div class="card"><table class="grid"><thead><tr>
      <th>#</th><th>帘片</th><th>轮次</th><th>内容</th><th>状态</th><th>操作/完工时间</th>
    </tr></thead><tbody>${repRows || '<tr><td colspan="6" class="empty">无修补记录</td></tr>'}</tbody></table></div>
    <div class="section-title mt">异常留痕（串包 / 缩水 / 染色 / 挂钩缺失 / 禁洗 / 延期 / 二次修补 / 不平整）</div>
    <div class="card">${incRows || '<div class="empty">无异常</div>'}</div>`;
}

/* ================= 视图六：配包配送 ================= */
function renderPack() {
  const el = $("#view-pack");
  // 待配包窗位：全部帘片 ready
  const readyWins = Object.values(STATE.windows).filter((w) => {
    const pcs = winPieces(w.id);
    return pcs.length && pcs.every((p) => p.status === "ready");
  });
  const readyRows = readyWins.map((w) => {
    const pcs = winPieces(w.id);
    return `<tr>
      <td><b>${wcode(w.id)}</b> ${esc(w.label)}</td>
      <td>${pcs.map((p) => `<span class="piece-chip"><span class="code">${esc(p.code)}</span></span>`).join(" ")}</td>
      <td><button class="act" onclick="doPack(${w.id})">按原窗位配包</button></td>
    </tr>`;
  }).join("");

  const pkgRows = STATE.packages.map((p) => {
    const pcs = Object.values(STATE.pieces).flat().filter((x) => x.package_id === p.id);
    return `<tr>
      <td><span class="seal">${esc(p.seal_no)}</span></td>
      <td>${wcode(p.window_id)}</td>
      <td>${pcs.map((x) => `<span class="piece-chip"><span class="code">${esc(x.code)}</span></span>`).join(" ")}</td>
      <td>${{ packing: "配包中", sealed: "已封包", delivering: "配送中", delivered: "已送达" }[p.status]}</td>
      <td>
        ${p.status === "packing" ? `<button class="act" onclick="doSealPkg(${p.id})">封包</button>` : ""}
        ${p.status === "sealed" ? `<label><input type="checkbox" class="pk-sel" value="${p.id}" data-order="${p.order_id}"> 加入批次</label>` : ""}
      </td></tr>`;
  }).join("");

  const dlvRows = STATE.deliveries.map((d) => `<tr>
    <td><span class="seal small">${esc(d.batch_no)}</span></td>
    <td>${esc(d.adapter_ref || "—")}</td>
    <td>${{ scheduled: "待发运", in_transit: "在途", delivered: "已送达", failed: "失败" }[d.status]}</td>
    <td class="muted">${esc((d.result || "").slice(0, 60))}</td>
    <td>${d.status === "in_transit" ? `<button class="act" onclick="doConfirmDlv(${d.id})">配送到户</button>` : ""}</td>
  </tr>`).join("");

  el.innerHTML = `
    <div class="section-title">配包 —— 洗净烘干后仍按原窗位配包，集齐方可封包</div>
    <div class="card"><table class="grid"><thead><tr>
      <th>窗位</th><th>帘片（全部完工）</th><th>操作</th>
    </tr></thead><tbody>${readyRows || '<tr><td colspan="3" class="empty">暂无待配包窗位</td></tr>'}</tbody></table></div>
    <div class="section-title mt">配送包 —— 以醒目封签号区分</div>
    <div class="card">
      <table class="grid"><thead><tr>
        <th>包封签号</th><th>窗位</th><th>帘片</th><th>状态</th><th>操作</th>
      </tr></thead><tbody>${pkgRows || '<tr><td colspan="5" class="empty">暂无配送包</td></tr>'}</tbody></table>
      <div class="mt">
        <button class="act" onclick="doCreateDelivery()">生成配送批次</button>
        <button class="act warn" onclick="doAdapterFail('delivery')">模拟配送拒单</button>
      </div>
    </div>
    <div class="section-title mt">配送批次</div>
    <div class="card"><table class="grid"><thead><tr>
      <th>批次号</th><th>物流单号</th><th>状态</th><th>适配器回执</th><th>操作</th>
    </tr></thead><tbody>${dlvRows || '<tr><td colspan="5" class="empty">暂无批次</td></tr>'}</tbody></table></div>`;
}

/* ================= 视图七：复装对照 ================= */
function renderReinstall() {
  const el = $("#view-reinstall");
  const wins = Object.values(STATE.windows).filter((w) =>
    ["reinstalling", "accepted"].includes(w.status));
  const html = wins.map((w) => {
    const pcs = winPieces(w.id);
    const cards = pcs.map((p) => {
      const lack = p.hooks_returned != null && p.hooks_returned < p.hooks_expected;
      return `<div class="card" style="margin-bottom:10px">
        <div class="row">
          <span class="piece-chip"><span class="code">${esc(p.code)}</span></span>
          ${pieceTag(p)}
          <span class="muted">${esc(p.fabric)}</span>
          <span class="grow"></span>
          ${["delivering", "reinstalling"].includes(p.status) ? `
            <input type="file" id="ph-${p.id}" accept="image/*" style="width:140px">
            <button class="act ghost" onclick="doUpload(${p.id},'reinstall')">传复装照</button>
            <button class="act" onclick="doReinstall(${p.id},${p.hooks_expected})">复装扫描</button>` : ""}
          ${p.status === "reinstalled" && w.status !== "accepted"
            ? `<button class="act warn" onclick="doUneven(${p.id})">复装不平整</button>` : ""}
        </div>
        <div class="compare mt">
          <div class="ph">
            <div class="ph-title">拆前照片</div>
            ${p.photo_before ? `<img src="${p.photo_before}" alt="拆前">` : '<div class="empty">无照片</div>'}
            <div class="hooks">拆卸挂钩：${p.hooks_counted ?? "—"} / 应 ${p.hooks_expected}</div>
          </div>
          <div class="ph">
            <div class="ph-title">复装后照片</div>
            ${p.photo_reinstall ? `<img src="${p.photo_reinstall}" alt="复装后">` : '<div class="empty">待复装扫描</div>'}
            <div class="hooks">复装挂钩：${p.hooks_returned ?? "—"} / 应 ${p.hooks_expected}
              ${lack ? '<span class="lack">（缺 ' + (p.hooks_expected - p.hooks_returned) + " 个）</span>" : ""}</div>
          </div>
        </div>
      </div>`;
    }).join("");
    const canAccept = w.status === "reinstalling";
    return `<div class="card tree-order">
      <h3>${wcode(w.id)} · ${esc(w.label)} ${winTag(w)}
        <span class="meta">${w.accepted_at ? "验收于 " + esc(w.accepted_at.slice(0, 16).replace("T", " ")) : ""}</span></h3>
      ${cards}
      ${canAccept ? `<button class="act ok" onclick="doAccept(${w.id})">窗位验收</button>` : ""}
    </div>`;
  }).join("");
  el.innerHTML = `<div class="section-title">复装对照 —— 拆前 / 复装后双图对照，挂钩数量见照片下方</div>
    ${html || '<div class="empty">暂无进入复装阶段的窗位</div>'}`;
}

/* ================= 视图八：费用复核 ================= */
function renderFees() {
  const el = $("#view-fees");
  const html = STATE.orders.map((o) => {
    const fees = STATE.fees.filter((f) => f.order_id === o.id);
    const pays = STATE.payments.filter((p) => p.order_id === o.id);
    const total = fees.reduce((s, f) => s + f.amount, 0);
    const feeRows = fees.map((f) => `<tr>
      <td>${esc(f.item)}</td>
      <td>${{ charge: "应收", discount: "优惠", compensation: "赔付" }[f.kind]}</td>
      <td class="${f.amount < 0 ? "neg" : ""}">${money(f.amount)}</td></tr>`).join("");
    const payRows = pays.map((p) => `<tr>
      <td>${esc(p.method)}</td><td>${money(p.amount)}</td>
      <td>${p.status === "success" ? '<span class="tag st-accepted">成功</span>' : '<span class="tag st-banned">失败</span>'}</td>
      <td class="muted">${esc(p.ref || "")}</td></tr>`).join("");
    return `<div class="card">
      <h3>${esc(o.code)} ${orderTag(o)}
        <span class="meta">${esc(o.customer_name)}｜${o.fees_reviewed ? "费用已复核" : "费用未复核"}</span></h3>
      <table class="grid"><thead><tr><th>项目</th><th>类型</th><th>金额</th></tr></thead>
        <tbody>${feeRows || '<tr><td colspan="3" class="empty">暂无费用</td></tr>'}</tbody></table>
      <div class="row mt">
        <span class="fee-total">合计 ${money(total)}</span><span class="grow"></span>
        <input id="fee-item-${o.id}" placeholder="费用项目" size="12">
        <input id="fee-amt-${o.id}" placeholder="金额" size="7" type="number" step="0.01">
        <button class="act ghost" onclick="doAddFee(${o.id})">加费用</button>
        <button class="act" onclick="doReviewFees(${o.id})" ${o.fees_reviewed ? "disabled" : ""}>复核通过</button>
        <button class="act ok" onclick="doPay(${o.id})">收款</button>
        <button class="act warn" onclick="doAdapterFail('payment')">模拟支付失败</button>
      </div>
      ${payRows ? `<table class="grid mt"><thead><tr><th>方式</th><th>金额</th><th>结果</th><th>单号</th></tr></thead><tbody>${payRows}</tbody></table>` : ""}
    </div>`;
  }).join("");
  el.innerHTML = `<div class="section-title">费用复核 —— 复核通过方可收款、关闭订单</div>${html}`;
}

/* ================= 提醒 ================= */
function renderReminderBadge() {
  const n = STATE.reminders.length;
  const badge = $("#reminder-count");
  badge.style.display = n ? "inline-block" : "none";
  badge.textContent = n;
  $("#reminder-list").innerHTML = STATE.reminders.map((r) =>
    `<div class="rem-item">${esc(r.message)}
      <div class="muted">${esc(r.fired_at.slice(0, 16).replace("T", " "))}
        <button class="act ghost" onclick="doAck(${r.id})">知道了</button></div>
    </div>`).join("") || '<div class="empty">暂无提醒</div>';
}

/* ================= 操作 ================= */
const actBtn = (label, fn, cls = "") => `<button class="act ${cls}" onclick="${fn}">${label}</button>`;
const run = (p, msg) => p.then(() => { toast(msg || "操作成功"); refresh(); })
  .catch((e) => toast(e.message, true));

window.doCreateOrder = () => run(api("/api/orders", {
  customer_name: $("#no-name").value || "未命名客户",
  phone: $("#no-phone").value || "-",
  address_alias: $("#no-alias").value || "未设代号",
  address_detail: $("#no-addr").value || "-",
  appointment_at: $("#no-appt").value ? new Date($("#no-appt").value).toISOString() : null,
}), "已建单");
window.doAddRoom = (oid) => { const name = prompt("房间名称（如 主卧）"); if (name) run(api("/api/rooms", { order_id: oid, name })); };
window.doAddWindow = (rid) => { const label = prompt("窗位名称（如 东窗 / 落地窗）"); if (label) run(api("/api/windows", { room_id: rid, label })); };
window.doAddPiece = (wid) => {
  const fabric = prompt("布料（如 棉麻混纺）"); if (!fabric) return;
  const hooks = parseInt(prompt("挂钩数量", "10") || "0", 10);
  const washable = confirm("该布料可水洗吗？（取消 = 禁洗）");
  const wash_req = prompt("洗护要求", washable ? "常规洗护" : "禁水洗，仅除尘") || "";
  run(api("/api/pieces", { window_id: wid, fabric, hooks, washable, wash_req }), "帘片已登记");
};
window.doStartRemoval = (oid) => run(api(`/api/orders/${oid}/start-removal`));
window.doCountHooks = (pid, expected) => {
  const n = parseInt(prompt(`清点挂钩数量（应有 ${expected}）`, String(expected)) || "", 10);
  if (Number.isNaN(n)) return;
  run(api(`/api/pieces/${pid}/count-hooks`, { hooks: n }), "挂钩已清点");
};
window.doSealBag = (wid) => run(api(`/api/windows/${wid}/seal-bag`).then((r) =>
  (toast(`已封袋，封签号 ${r.seal_no}`), refresh())), "");
window.doTransport = (oid) => run(api(`/api/orders/${oid}/transport`), "已发运");
window.doReceive = (oid) => run(api(`/api/orders/${oid}/receive`), "已到厂");
window.doPostpone = (oid) => {
  const days = parseInt(prompt("延期天数", "1") || "0", 10); if (!days) return;
  const reason = prompt("延期原因", "客户临时有事") || "";
  run(api(`/api/orders/${oid}/postpone`, { days, reason }), "已登记延期");
};
window.doClose = (oid) => confirm("确认关闭订单？需全部窗位验收且费用已复核。") &&
  run(api(`/api/orders/${oid}/close`), "订单已关闭");
window.doWash = (pid) => run(api(`/api/pieces/${pid}/wash`));
window.doSkipWash = (pid) => run(api(`/api/pieces/${pid}/skip-wash`, {}), "已隔洗");
window.doWashFinish = (pid) => run(api(`/api/pieces/${pid}/wash-finish`));
window.doDry = (pid) => run(api(`/api/pieces/${pid}/dry`));
window.doDryFinish = (pid) => run(api(`/api/pieces/${pid}/dry-finish`));
window.doReady = (pid) => run(api(`/api/pieces/${pid}/ready`), "已完工待配包");
window.doMisbag = (pid) => {
  const seal = prompt("发现该帘片的封签号（非本窗位袋）", "SEAL-B-"); if (!seal) return;
  const note = prompt("情况说明", "到厂拆袋发现串包") || "";
  run(api(`/api/pieces/${pid}/misbag`, { found_seal: seal, note }), "串包已留痕");
};
window.doIncident = (pid) => {
  const type = prompt("异常类型：shrinkage缩水 / dye染色 / hooks_missing挂钩缺失", "shrinkage");
  if (!type) return;
  const note = prompt("情况说明", "") || "";
  run(api(`/api/pieces/${pid}/incident`, { type, note }), "异常已留痕");
};
window.doDamage = (pid) => {
  const d = prompt("损伤描述（将由客服主管审批赔付/返工）"); if (!d) return;
  run(api(`/api/pieces/${pid}/damage`, { description: d }), "损伤已登记，待主管审批");
};
window.doRepair = (pid) => {
  const note = prompt("修补内容"); if (!note) return;
  run(api(`/api/pieces/${pid}/repair`, { note }), "已开修补单");
};
window.doFinishRepair = (rid) => run(api(`/api/repairs/${rid}/finish`), "修补完工");
window.doDecide = (did, decision) => {
  let amount = 0, note = "";
  if (decision === "compensate") {
    amount = parseFloat(prompt("赔付金额", "50") || "0");
    note = prompt("赔付说明", "") || "";
  } else if (decision === "rework") {
    note = prompt("返工要求", "返工处理") || "";
  } else if (!confirm("确认驳回该损伤登记？")) return;
  run(api(`/api/damages/${did}/decide`, { decision, amount, note }), "已审批");
};
window.doResolve = (iid) => {
  const note = prompt("处理结果", "已处理") || "";
  run(api(`/api/incidents/${iid}/resolve`, { note }), "已销项");
};
window.doPack = (wid) => run(api(`/api/windows/${wid}/pack`).then((r) =>
  (toast(`配包完成，封签号 ${r.seal_no}`), refresh())), "");
window.doSealPkg = (pid) => run(api(`/api/packages/${pid}/seal`), "已封包");
window.doCreateDelivery = () => {
  const sel = $$(".pk-sel:checked");
  if (!sel.length) return toast("请先勾选配送包", true);
  const oid = sel[0].dataset.order;
  if (sel.some((s) => s.dataset.order !== oid)) return toast("同一批次须属于同一订单", true);
  run(api(`/api/orders/${oid}/deliver`, { package_ids: sel.map((s) => +s.value) }).then((r) =>
    (toast(`批次 ${r.batch_no} 已发运`), refresh())), "");
};
window.doConfirmDlv = (did) => run(api(`/api/deliveries/${did}/confirm`), "已送达");
window.doReinstall = (pid, expected) => {
  const n = parseInt(prompt(`复装挂钩核对（应有 ${expected}）`, String(expected)) || "", 10);
  if (Number.isNaN(n)) return;
  run(api(`/api/pieces/${pid}/reinstall`, { hooks: n }), "复装扫描成功");
};
window.doUneven = (pid) => {
  const note = prompt("不平整情况", "左高右低，需调整"); if (!note) return;
  run(api(`/api/pieces/${pid}/uneven`, { note }), "已留痕，帘片退回待调整");
};
window.doAccept = (wid) => confirm("确认该窗位全部帘片复装合格？") &&
  run(api(`/api/windows/${wid}/accept`), "窗位验收通过");
window.doAddFee = (oid) => {
  const item = $(`#fee-item-${oid}`).value, amount = parseFloat($(`#fee-amt-${oid}`).value);
  if (!item || Number.isNaN(amount)) return toast("请填写费用项目与金额", true);
  const kind = amount < 0 ? (confirm("负金额：确定为「赔付」？（取消=优惠）") ? "compensation" : "discount") : "charge";
  run(api("/api/fees", { order_id: oid, item, amount, kind }), "费用已添加");
};
window.doReviewFees = (oid) => run(api(`/api/orders/${oid}/review-fees`), "费用复核通过");
window.doPay = (oid) => {
  const method = prompt("收款方式", "微信支付") || "微信支付";
  run(api(`/api/orders/${oid}/pay`, { method }), "收款成功");
};
window.doAdapterFail = (which) => run(api("/api/demo/adapter-fail", { which }), "已注入：下一次适配器调用将失败");
window.doAck = (rid) => run(api(`/api/reminders/${rid}/ack`), "");

window.doUpload = async (pid, kind) => {
  const input = $(`#ph-${pid}`);
  if (!input || !input.files.length) return toast("请先选择照片文件", true);
  try {
    const res = await fetch(`/api/pieces/${pid}/photo?kind=${kind}&actor=${ROLE}`, {
      method: "POST",
      headers: { "Content-Type": input.files[0].type || "application/octet-stream" },
      body: input.files[0],
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "上传失败");
    toast("照片已上传");
    refresh();
  } catch (e) { toast(e.message, true); }
};

/* ================= 框架 ================= */
function renderAll() {
  renderOrders(); renderRemoval(); renderWindows(); renderWash();
  renderRepair(); renderPack(); renderReinstall(); renderFees();
}

$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  $$("#nav button").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + btn.dataset.view));
});

$("#role").value = ROLE;
$("#role").addEventListener("change", (e) => {
  ROLE = e.target.value;
  localStorage.setItem("cc-role", ROLE);
  toast(`已切换角色：${e.target.selectedOptions[0].text}`);
});

$("#reminder-btn").addEventListener("click", () => {
  const p = $("#reminder-panel");
  p.style.display = p.style.display === "block" ? "none" : "block";
});

refresh().catch((e) => toast(e.message, true));
setInterval(() => refresh().catch(() => {}), 15000);
