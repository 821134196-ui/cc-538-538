/* 绿园树木认养 —— 前端逻辑（无框架） */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  const state = {
    users: [], user: null, trees: [], detailId: null,
    cal: new Date((new Date()).getFullYear(), (new Date()).getMonth(), 1),
    holdTimer: null,
  };

  const KIND_LABEL = { water: "浇水", prune: "修剪", pest_check: "病虫观察" };
  const HEALTH_META = {
    healthy:     { label: "健康", fill: "#3a7d50" },
    monitor:     { label: "观察中", fill: "#c0953c" },
    sick_locked: { label: "患病封控", fill: "#b2473e" },
    recovering:  { label: "复壮中", fill: "#7faf8e" },
    transplanted:{ label: "已移栽", fill: "#8fae98" },
    removed:     { label: "已移除", fill: "#a7b0aa" },
  };
  const SEASONS = [
    { name: "春季", months: [3, 4, 5], icon: "🌱" },
    { name: "夏季", months: [6, 7, 8], icon: "🌿" },
    { name: "秋季", months: [9, 10, 11], icon: "🍂" },
    { name: "冬季", months: [12, 1, 2], icon: "❄️" },
  ];

  // ------------------------------------------------------------- 工具
  async function api(method, path, body) {
    const opt = {
      method,
      headers: { "Content-Type": "application/json", "X-User-Id": String(state.user.id) },
    };
    if (body) opt.body = JSON.stringify(body);
    const res = await fetch(path, opt);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
    return data;
  }
  const get = (p) => api("GET", p);
  const post = (p, b = {}) => api("POST", p, b);

  function toast(msg, isErr = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isErr ? " err" : "");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), 3200);
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtDate = (s) => s ? s.slice(0, 10) : "—";
  const seasonOf = (dateStr) => {
    const m = Number(dateStr.slice(5, 7));
    return SEASONS.find((x) => x.months.includes(m));
  };
  function pill(status) {
    const map = { scheduled: "待执行", done: "已完成", overdue: "已逾期", cancelled: "已取消" };
    return `<span class="pill ${status}">${map[status] || status}</span>`;
  }

  // ------------------------------------------------------------- 身份切换
  async function initUsers() {
    state.users = await get("/api/users");
    const sel = $("#user-select");
    const saved = Number(localStorage.getItem("tree-user") || 1);
    sel.innerHTML = state.users.map(
      (u) => `<option value="${u.id}">${u.name} · ${
        { resident: "居民", gardener: "园艺师", supervisor: "主管" }[u.role]}</option>`).join("");
    sel.value = state.users.some((u) => u.id === saved) ? saved : state.users[0].id;
    state.user = state.users.find((u) => u.id === Number(sel.value));
    sel.onchange = () => {
      state.user = state.users.find((u) => u.id === Number(sel.value));
      localStorage.setItem("tree-user", state.user.id);
      applyRoleUI();
      refreshAll();
    };
    applyRoleUI();
  }

  function applyRoleUI() {
    const staff = state.user.role !== "resident";
    $("#tab-review").style.display = staff ? "" : "none";
    if (!staff && $("#view-review").classList.contains("active")) {
      switchTab("map");
    }
  }

  $$(".tab").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));
  function switchTab(name) {
    $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    if (name === "calendar") loadCalendar();
    if (name === "review") loadReview();
    if (name === "mine") loadMine();
  }

  // ------------------------------------------------------------- 地图
  async function loadMap() {
    state.trees = await get("/api/trees/map");
    const svg = $("#map");
    // 步道（固定手绘路径，坐标空间 0..200 × 0..140）
    const paths = `
      <path d="M8 132 C 60 118, 90 96, 120 74 S 176 34, 196 12"
            fill="none" stroke="#e8e2cd" stroke-width="7" stroke-linecap="round"/>
      <path d="M30 14 C 78 34, 120 40, 190 46" fill="none" stroke="#ece6d2"
            stroke-width="5" stroke-linecap="round"/>
      <path d="M24 128 C 70 104, 120 102, 176 120" fill="none" stroke="#e8e2cd"
            stroke-width="5" stroke-linecap="round"/>
      <rect x="6" y="6" width="188" height="128" rx="10" fill="none"
            stroke="#d9e8de" stroke-dasharray="2 3"/>
      <text x="14" y="20" font-size="5" fill="#8aa094">阳光步道</text>
      <text x="150" y="42" font-size="5" fill="#8aa094">银杏道</text>`;

    const nodes = state.trees.map((t) => {
      const meta = HEALTH_META[t.health];
      let ring = "#cfe0d5", dash = "", cls = "", width = 1.1;
      if (t.adopt_state === "active") { ring = "#2e6d45"; width = 2.2; }
      if (t.adopt_state === "mine") { ring = "#e27a99"; width = 3; }
      if (t.adopt_state === "held") { ring = "#c0953c"; dash = "3 2"; cls = "hold-ring"; }
      if (t.adopt_state === "held_by_me") { ring = "#e27a99"; dash = "3 2"; cls = "hold-ring"; }
      if (t.adopt_state === "locked") { ring = "#b2473e"; width = 2.2; }
      const arrow = t.transplant
        ? `<line x1="${t.transplant.old_x}" y1="${t.transplant.old_y}"
                 x2="${t.x - 4}" y2="${t.y - 4}" stroke="#b08a3e" stroke-width=".8"
                 stroke-dasharray="2 2" marker-end="url(#arr)"/>
           <circle cx="${t.transplant.old_x}" cy="${t.transplant.old_y}" r="1.6"
                   fill="none" stroke="#b08a3e" stroke-width=".7"/>`
        : "";
      const cross = t.health === "removed" ? '<path d="M-3 -3 L3 3 M3 -3 L-3 3" stroke="#fff" stroke-width="1.2"/>' : "";
      return `
        <g class="tree-node" data-id="${t.id}" transform="translate(${t.x} ${t.y})">
          ${arrow}
          <circle class="canopy ${cls}" r="7.2" fill="${meta.fill}" stroke="${ring}"
                  stroke-width="${width}" stroke-dasharray="${dash}"/>
          ${cross}
          <text y="14" text-anchor="middle" font-size="4.6" fill="#41524a">${t.id}</text>
          <title>${t.id} ${t.species}｜健康：${meta.label}｜${adoptLabel(t)}</title>
        </g>`;
    }).join("");

    svg.innerHTML = `
      <defs><marker id="arr" markerWidth="5" markerHeight="5" refX="4" refY="2.5"
        orient="auto"><path d="M0,0 L5,2.5 L0,5 z" fill="#b08a3e"/></marker></defs>
      ${paths}${nodes}`;
    $$(".tree-node", svg).forEach((g) => (g.onclick = () => loadDetail(g.dataset.id)));

    $("#map-legend").innerHTML = [
      ...Object.entries(HEALTH_META).map(([k, v]) =>
        `<span><i style="background:${v.fill}"></i>${v.label}</span>`),
      `<span><i style="background:#fff;border:2px solid #2e6d45"></i>认养中</span>`,
      `<span><i style="background:#fff;border:2px dashed #c0953c"></i>占位锁定</span>`,
      `<span><i style="background:#fff;border:2px solid #e27a99"></i>我的认养</span>`,
    ].join("");
  }

  function adoptLabel(t) {
    return {
      available: "可认养", active: "已被认养", mine: "我的认养",
      held: "占位待支付", held_by_me: "我的占位（待支付）", locked: "封控不可认养",
    }[t.adopt_state];
  }

  // ------------------------------------------------------------- 树木详情
  async function loadDetail(id) {
    state.detailId = id;
    const d = await get(`/api/trees/${id}`);
    const isResident = state.user.role === "resident";
    const staff = !isResident;
    const a = d.current_adoption;
    const mine = a && !a.anonymized && a.user_id === state.user.id;

    const holdBox = a && a.status === "pending" && mine
      ? `<div class="countdown" id="hold-count">支付剩余 --:--</div>` : "";

    // 认养状态（与健康状态分开两块）
    let adoptBlock;
    if (!a) {
      const canApply = isResident && !["sick_locked", "removed"].includes(d.health_status);
      adoptBlock = canApply ? applyForm(d) :
        `<p class="kv">当前无可认养名额${isResident ? "（树木封控/移除中）" : ""}。</p>`;
    } else if (a.anonymized) {
      adoptBlock = `<div class="kv">认养状态：<b>${a.status_label}</b>（${a.start_date} ~ ${a.end_date}）
        <br/><span class="hint">出于隐私保护，他人认养信息已脱敏</span></div>`;
    } else {
      const btns = [];
      if (mine && a.status === "pending")
        btns.push(`<button class="btn small" id="btn-pay">立即支付（模拟）</button>`);
      if (mine && a.status === "active")
        btns.push(`<button class="btn danger small" id="btn-exit">退出认养</button>`);
      adoptBlock = `
        <div class="kv">认养状态：<b>${a.status_label}</b> · ${a.care_mode_label}</div>
        <div class="kv">认养人：<b>${esc(a.user_name)}</b>｜${a.start_date} ~ ${a.end_date}</div>
        ${holdBox}
        <div class="actions" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">${btns.join("")}</div>`;
    }

    const moves = d.transplants.length
      ? d.transplants.map((m) =>
          `<div class="move-note">🚚 ${m.happened_on} 移栽：(${m.old_x}, ${m.old_y})
           → (${m.new_x}, ${m.new_y})${m.reason ? "。" + esc(m.reason) : ""}</div>`).join("")
      : "";

    // 养护记录按季节排列
    const taskGroups = SEASONS.map((s) => {
      const items = d.tasks.filter((t) => {
        const y = Number(t.due_date.slice(0, 4));
        return s.months.includes(Number(t.due_date.slice(5, 7))) &&
               t.status !== "cancelled";
      });
      if (!items.length) return "";
      // 用第一条任务的年份标记
      const year = items[0].due_date.slice(0, 4);
      return `<div class="section-title">${year} · ${s.icon} ${s.name}</div>` +
        items.map((t) => `
          <div class="task-row">
            <span class="date">${fmtDate(t.due_date)}</span>
            <span class="kind">${KIND_LABEL[t.kind]}</span>
            ${pill(t.status)}
            <span class="hint">${t.assignee === "gardener" ? "园艺师" : "认养人"}
              ${t.done_date ? "· 完成于 " + fmtDate(t.done_date) : ""}
              ${t.note ? "· " + esc(t.note) : ""}</span>
          </div>`).join("");
    }).join("");

    const photos = d.photos.length
      ? `<div class="photo-grid">${d.photos.map((p) => `
          <figure>
            <img src="/photos/${p.filename}" alt="${esc(p.caption)}" loading="lazy"
                 title="拍摄 ${p.taken_on}｜亮度 ${p.brightness}｜${esc(p.uploader_name)}"/>
            <figcaption>${p.taken_on} ${esc(p.caption)}</figcaption>
          </figure>`).join("")}</div>`
      : `<p class="hint">暂无巡查照片</p>`;

    const events = d.events.slice(0, 12).map((e) => `
      <li><div>${esc(e.detail)}</div><div class="t">${(e.created_at || "").replace("T", " ").slice(0, 16)}</div></li>`
    ).join("");

    const anomalies = d.anomalies.length
      ? d.anomalies.map((r) => `
          <div class="anomaly">
            <div class="top"><b>#${r.id} 异常记录</b>
              <span class="st-${r.status === "needs_review" ? "review" : r.status}">
                ${anomalyStatus(r.status)}</span></div>
            <div class="meta">类型：${kindText(r.kind)}｜${(r.created_at || "").slice(0, 10)}</div>
            ${r.detail ? `<p>${esc(r.detail)}</p>` : ""}
            ${r.diagnosis ? `<p class="kv">园艺师诊断：<b>${esc(r.diagnosis)}</b></p>` : ""}
            ${r.review_note ? `<p class="kv">主管意见：${esc(r.review_note)}</p>` : ""}
          </div>`).join("")
      : "";

    $("#tree-detail").innerHTML = `
      <div class="d-head">
        <h3>${esc(d.species)}</h3><span class="tid">${d.id}</span>
      </div>
      <p class="kv" style="margin-top:6px">📍 位置 (${d.x}, ${d.y})　可认养期限：${d.adoptable_from} ~ ${d.adoptable_to}</p>
      <p class="kv">${esc(d.notes)}</p>

      <div class="status-rows">
        <div class="status-box health">
          <div class="k">专业健康结论（园艺师诊断 · 主管复核）</div>
          <div class="v" style="color:${HEALTH_META[d.health_status].fill}">
            ${HEALTH_META[d.health_status].label}</div>
        </div>
        <div class="status-box">
          <div class="k">认养状态</div>
          <div class="v">${a ? a.status_label : "可认养"}</div>
        </div>
      </div>
      ${moves}
      <div id="adopt-block">${adoptBlock}</div>

      ${isResident ? residentReportForm(d) : ""}
      ${staff ? staffActions(d) : ""}

      <div class="section-title">📷 巡查照片（平均亮度需 ≥ 0.42 才可归档）</div>
      ${photos}
      ${uploader(d)}

      <div class="section-title">🗓 养护记录（按季节排列）</div>
      ${taskGroups || '<p class="hint">暂无计划任务</p>'}

      ${anomalies ? `<div class="section-title">🔎 异常记录</div>${anomalies}` : ""}

      <div class="section-title">📜 档案事件流</div>
      <ul class="timeline">${events || "<li>暂无事件</li>"}</ul>`;

    // 事件绑定
    const btnApply = $("#btn-apply");
    if (btnApply) btnApply.onclick = () => submitApply(d);
    const btnPay = $("#btn-pay");
    if (btnPay) {
      btnPay.onclick = () => pay(a.id);
      startHoldCountdown(a.hold_expires, a.id);
    }
    const btnExit = $("#btn-exit");
    if (btnExit) btnExit.onclick = () => exitAdopt(a.id);
    const btnReport = $("#btn-report");
    if (btnReport) btnReport.onclick = () => submitReport(d.id);
    const btnMove = $("#btn-transplant");
    if (btnMove) btnMove.onclick = () => submitTransplant(d);
    const btnReopen = $("#btn-reopen");
    if (btnReopen) btnReopen.onclick = () => reopen(d.id);
    bindUploader(d.id);
  }

  function applyForm(d) {
    const today = new Date().toISOString().slice(0, 10);
    return `
      <div class="form-grid">
        <label class="fld">开始日期<input type="date" id="ap-start" min="${d.adoptable_from}"
          max="${d.adoptable_to}" value="${today}"></label>
        <label class="fld">结束日期<input type="date" id="ap-end" min="${d.adoptable_from}"
          max="${d.adoptable_to}"></label>
        <label class="fld full">养护方式
          <select id="ap-mode">
            <option value="watering">仅浇水协助（认养人完成浇水）</option>
            <option value="full_care">全程养护（浇水也由园艺师安排）</option>
          </select></label>
      </div>
      <button class="btn small" id="btn-apply">提交申请并锁定名额（30 分钟内支付）</button>
      <p class="hint">并发申请按提交时间先到先得；逾期未支付名额自动释放。</p>`;
  }

  function residentReportForm(d) {
    return `
      <div class="section-title">⚠️ 发现异常？（居民上报，不构成健康结论）</div>
      <div class="form-grid">
        <label class="fld">现象
          <select id="rp-kind">
            <option value="wilt">疑似枯萎</option>
            <option value="pest">虫害迹象</option>
            <option value="disease">病斑/异常</option>
            <option value="branch_damage">枝干损伤</option>
            <option value="other">其他</option>
          </select></label>
        <label class="fld full">描述<input id="rp-detail" placeholder="如：午后叶片发蔫，位置在靠步道一侧"></label>
      </div>
      <button class="btn ghost small" id="btn-report">提交给园艺师核实</button>`;
  }

  function staffActions(d) {
    const reopen = d.health_status === "sick_locked"
      ? `<button class="btn ghost small" id="btn-reopen">解除封控并恢复计划</button>` : "";
    return `
      <div class="section-title">🧑‍🌾 园艺操作</div>
      <div class="form-grid">
        <label class="fld">新坐标 X<input type="number" id="tp-x" value="${d.x}" min="10" max="190" step="1"></label>
        <label class="fld">新坐标 Y<input type="number" id="tp-y" value="${d.y}" min="10" max="130" step="1"></label>
        <label class="fld full">移栽/登记原因<input id="tp-reason" placeholder="如：避让施工，移栽复壮"></label>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn small" id="btn-transplant">登记移栽并重算计划</button>${reopen}
      </div>`;
  }

  function kindText(k) {
    return { wilt: "疑似枯萎", pest: "虫害", disease: "病害", branch_damage: "枝干损伤",
             transplant_shock: "移栽应激", other: "其他" }[k] || k;
  }
  function anomalyStatus(s) {
    return { open: "待园艺师核实", needs_review: "待主管复核", approved: "已批准",
             rejected: "已驳回", false_alarm: "误报已归档" }[s] || s;
  }

  // ------------------------------------------------------------- 申请/支付/退出
  async function submitApply(d) {
    try {
      const body = {
        start_date: $("#ap-start").value, end_date: $("#ap-end").value,
        care_mode: $("#ap-mode").value,
      };
      if (!body.end_date) throw new Error("请选择认养结束日期");
      const r = await post(`/api/trees/${d.id}/adoptions`, body);
      toast(`名额已锁定为认养单 #${r.id}，请在 30 分钟内支付`);
      await refreshAll();
    } catch (e) { toast(e.message, true); }
  }

  async function pay(id) {
    try {
      await post(`/api/adoptions/${id}/pay`);
      toast("支付成功（模拟网关），养护计划已生成，已通知园艺师");
      await refreshAll();
    } catch (e) { toast(e.message, true); }
  }

  async function exitAdopt(id) {
    const reason = prompt("退出原因（可选）：", "个人时间安排");
    if (reason === null) return;
    try {
      await post(`/api/adoptions/${id}/exit`, { reason });
      toast("已退出认养，未执行计划终止，费用模拟原路退回");
      await refreshAll();
      if ($("#view-mine").classList.contains("active")) loadMine();
    } catch (e) { toast(e.message, true); }
  }

  async function submitReport(treeId) {
    try {
      await post(`/api/trees/${treeId}/anomalies`,
        { kind: $("#rp-kind").value, detail: $("#rp-detail").value });
      toast("已提交，园艺师将现场核实（您的上报不会直接改变健康结论）");
      await loadDetail(treeId);
    } catch (e) { toast(e.message, true); }
  }

  async function submitTransplant(d) {
    try {
      const body = {
        new_x: Number($("#tp-x").value), new_y: Number($("#tp-y").value),
        reason: $("#tp-reason").value,
      };
      await post(`/api/trees/${d.id}/transplant`, body);
      toast("移栽已登记：坐标更新，复壮期计划已重算，认养人已收到通知");
      await refreshAll();
    } catch (e) { toast(e.message, true); }
  }

  async function reopen(id) {
    try {
      await post(`/api/trees/${id}/reopen`, { target_health: "recovering" });
      toast("已解除封控，养护计划恢复");
      await refreshAll();
    } catch (e) { toast(e.message, true); }
  }

  function startHoldCountdown(expires, adoptionId) {
    clearInterval(state.holdTimer);
    const el = () => $("#hold-count");
    const tick = () => {
      const left = Math.max(0, Math.floor((new Date(expires) - new Date()) / 1000));
      const node = el();
      if (!node) { clearInterval(state.holdTimer); return; }
      const mm = String(Math.floor(left / 60)).padStart(2, "0");
      const ss = String(left % 60).padStart(2, "0");
      node.textContent = `支付剩余 ${mm}:${ss}`;
      if (left === 0) {
        clearInterval(state.holdTimer);
        toast("占位已到期释放，请重新申请");
        refreshAll();
      }
    };
    tick();
    state.holdTimer = setInterval(tick, 1000);
  }

  // ------------------------------------------------------------- 巡查照片上传
  function uploader(d) {
    return `
      <div class="uploader">
        <label class="btn ghost small" for="photo-file">＋ 上传巡查照片（本地 PNG）</label>
        <input type="file" id="photo-file" accept="image/png">
        <label class="fld" style="margin-top:8px">说明<input id="photo-caption" placeholder="如：浇水后留影 / 病叶特写"></label>
        <canvas class="preview hidden" id="photo-canvas"></canvas>
        <div id="photo-bright"></div>
        <div style="margin-top:8px;display:flex;gap:8px;justify-content:center">
          <button class="btn small" id="photo-submit" disabled>归档照片</button>
          <button class="btn ghost small hidden" id="photo-fix">一键补光后重检</button>
        </div>
      </div>`;
  }

  let photoCtx = null;
  function bindUploader(treeId) {
    const input = $("#photo-file"), canvas = $("#photo-canvas");
    photoCtx = null;
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) return;
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, 320 / img.width, 240 / img.height);
        canvas.width = Math.max(1, Math.round(img.width * scale));
        canvas.height = Math.max(1, Math.round(img.height * scale));
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        photoCtx = ctx;
        canvas.classList.remove("hidden");
        checkBrightness();
      };
      img.src = URL.createObjectURL(file);
    };
    $("#photo-fix").onclick = () => {
      if (!photoCtx) return;
      const imgData = photoCtx.getImageData(0, 0, canvas.width, canvas.height);
      let gain = 1.25;
      for (let i = 0; i < 6; i++) {
        const d = imgData.data;
        for (let p = 0; p < d.length; p += 4) {
          d[p] = Math.min(255, d[p] * gain);
          d[p + 1] = Math.min(255, d[p + 1] * gain);
          d[p + 2] = Math.min(255, d[p + 2] * gain);
        }
        photoCtx.putImageData(imgData, 0, 0);
        if (measureBrightness(photoCtx) >= 0.48) break;
      }
      checkBrightness();
    };
    $("#photo-submit").onclick = async () => {
      try {
        await post(`/api/trees/${treeId}/photos`, {
          data_url: canvas.toDataURL("image/png"),
          caption: $("#photo-caption").value || "巡查照片",
          taken_on: new Date().toISOString().slice(0, 10),
        });
        toast("照片已归档");
        await loadDetail(treeId);
      } catch (e) { toast(e.message, true); }
    };
  }

  function measureBrightness(ctx) {
    const { data, width, height } = ctx.getImageData(0, 0, ctx.canvas.width, ctx.canvas.height);
    let sum = 0, n = 0;
    for (let i = 0; i < data.length; i += 12) {  // 抽样
      sum += 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      n++;
    }
    return sum / n / 255;
  }

  function checkBrightness() {
    const b = measureBrightness(photoCtx);
    const ok = b >= 0.42;
    $("#photo-bright").innerHTML = ok
      ? `<span class="hint">平均亮度 ${b.toFixed(2)}，可辨识 ✓</span>`
      : `<div class="bright-warn">平均亮度 ${b.toFixed(2)} &lt; 0.42，画面过暗不可归档，请补光后重拍</div>`;
    $("#photo-submit").disabled = !ok;
    $("#photo-fix").classList.toggle("hidden", ok);
  }

  // ------------------------------------------------------------- 养护日历
  async function loadCalendar() {
    const month = `${state.cal.getFullYear()}-${String(state.cal.getMonth() + 1).padStart(2, "0")}`;
    $("#cal-title").textContent = `${state.cal.getFullYear()} 年 ${state.cal.getMonth() + 1} 月`;
    const tasks = await get(`/api/calendar?month=${month}`);

    const first = new Date(state.cal.getFullYear(), state.cal.getMonth(), 1);
    const days = new Date(state.cal.getFullYear(), state.cal.getMonth() + 1, 0).getDate();
    const startDow = first.getDay();
    const todayStr = new Date().toISOString().slice(0, 10);
    const byDay = {};
    tasks.forEach((t) => (byDay[t.due_date] ||= []).push(t));

    const dows = ["日", "一", "二", "三", "四", "五", "六"];
    let html = '<div class="cal-grid">' + dows.map((d) => `<div class="dow">${d}</div>`).join("");
    for (let i = 0; i < startDow; i++) html += `<div class="cal-cell out"></div>`;
    for (let day = 1; day <= days; day++) {
      const ds = `${month}-${String(day).padStart(2, "0")}`;
      const chips = (byDay[ds] || []).map((t) =>
        `<div class="cal-chip ${t.kind} ${t.status === "overdue" ? "overdue" : ""} ${t.status === "done" ? "done" : ""}"
          title="${t.tree_id}｜${KIND_LABEL[t.kind]}｜${pillText(t.status)}${t.note ? "｜" + esc(t.note) : ""}">
          ${t.tree_id} ${KIND_LABEL[t.kind]}</div>`).join("");
      html += `<div class="cal-cell ${ds === todayStr ? "today" : ""}"><span class="n">${day}</span>${chips}</div>`;
    }
    html += "</div>";

    const actionable = tasks.filter((t) => t.can_complete &&
      ["scheduled", "overdue"].includes(t.status));
    html += `<div class="section-title">✅ 可执行任务（${actionable.length}）</div>` +
      (actionable.length ? actionable.map((t) => `
        <div class="task-row">
          <span class="date">${t.due_date}</span>
          <span style="width:52px">${t.tree_id}</span>
          <span class="kind">${KIND_LABEL[t.kind]}</span>
          ${pill(t.status)}
          <button class="btn small" data-task="${t.id}" style="margin-left:auto">登记完成</button>
        </div>`).join("")
        : '<p class="hint">本月没有需要您执行的待办（他人负责的任务仍在日历中标出）</p>');

    $("#calendar-body").innerHTML = html;
    $$("#calendar-body [data-task]").forEach((btn) => (btn.onclick = async () => {
      const note = prompt("执行备注（可留空）：", "") ?? "";
      try {
        await post(`/api/tasks/${btn.dataset.task}/complete`, { note });
        toast("执行记录已写入");
        loadCalendar();
      } catch (e) { toast(e.message, true); }
    }));
  }
  const pillText = (s) => ({ scheduled: "待执行", done: "已完成", overdue: "已逾期", cancelled: "已取消" }[s]);

  $("#cal-prev").onclick = () => { state.cal.setMonth(state.cal.getMonth() - 1); loadCalendar(); };
  $("#cal-next").onclick = () => { state.cal.setMonth(state.cal.getMonth() + 1); loadCalendar(); };

  // ------------------------------------------------------------- 异常复核
  async function loadReview() {
    if (state.user.role === "resident") return;
    const q = await get("/api/reviews");

    $("#gardener-queue-card").style.display =
      state.user.role === "gardener" || q.open_reports.length ? "" : "none";
    $("#supervisor-queue-card").style.display =
      state.user.role === "supervisor" || q.review_queue.length ? "" : "none";

    $("#gardener-queue").innerHTML = q.open_reports.length
      ? q.open_reports.map((r) => reportCard(r, "open")).join("")
      : '<p class="empty">暂无待核实的居民上报</p>';
    $("#supervisor-queue").innerHTML = q.review_queue.length
      ? q.review_queue.map((r) => reportCard(r, "review")).join("")
      : '<p class="empty">暂无待复核的园艺师诊断</p>';

    $$("[data-false]").forEach((b) => (b.onclick = async () => {
      const diagnosis = prompt("核实意见（为何判定为误报）：", "现场查看枝叶挺立，无枯死迹象，判断为高温生理性萎蔫");
      if (!diagnosis) return;
      try {
        await post(`/api/anomalies/${b.dataset.false}/diagnose`,
          { is_real: false, diagnosis });
        toast("已归档为误报，7 天后自动安排复查任务");
        loadReview();
      } catch (e) { toast(e.message, true); }
    }));
    $$("[data-diag]").forEach((b) => (b.onclick = async () => {
      const diagnosis = prompt("园艺师诊断意见：", "");
      if (!diagnosis) return;
      const target = $(`#target-${b.dataset.diag}`).value;
      try {
        await post(`/api/anomalies/${b.dataset.diag}/diagnose`,
          { is_real: true, diagnosis, proposed_health: target });
        toast("诊断已提交，等待主管复核后才会生效");
        loadReview();
      } catch (e) { toast(e.message, true); }
    }));
    $$("[data-approve],[data-reject]").forEach((b) => (b.onclick = async () => {
      const approve = b.hasAttribute("data-approve");
      const note = prompt(approve ? "批准意见（可留空）：" : "驳回理由：", "") ?? "";
      if (note === null) return;
      try {
        await post(`/api/anomalies/${b.dataset.approve || b.dataset.reject}/review`,
          { approve, review_note: note });
        toast(approve ? "已批准：健康结论与计划已联动更新" : "已驳回，维持原健康结论");
        loadReview();
      } catch (e) { toast(e.message, true); }
    }));
  }

  function reportCard(r, mode) {
    const targets = Object.entries(HEALTH_META)
      .filter(([k]) => ["monitor", "sick_locked", "recovering", "healthy"].includes(k))
      .map(([k, v]) => `<option value="${k}" ${k === "monitor" ? "selected" : ""}>${v.label}</option>`)
      .join("");
    const gardenerActions = `
      <div class="actions">
        <label class="fld" style="flex:1;min-width:160px">拟变更健康结论
          <select id="target-${r.id}">${targets}</select></label>
      </div>
      <div class="actions">
        <button class="btn small" data-diag="${r.id}">确认异常 · 提交诊断送复核</button>
        <button class="btn ghost small" data-false="${r.id}">判定误报（自动安排复查）</button>
      </div>`;
    const supervisorActions = `
      <div class="actions">
        <button class="btn small" data-approve="${r.id}">批准并生效</button>
        <button class="btn danger small" data-reject="${r.id}">驳回诊断</button>
      </div>`;
    return `
      <div class="anomaly">
        <div class="top"><b>${r.tree_id} · ${kindText(r.kind)}</b>
          <span class="st-review">${anomalyStatus(r.status)}</span></div>
        <div class="meta">上报人：${esc(r.reporter_name)}｜${(r.created_at || "").slice(0, 10)}</div>
        <p>${esc(r.detail)}</p>
        ${r.diagnosis ? `<p class="kv">园艺师诊断：<b>${esc(r.diagnosis)}</b>
          ${r.proposed_health ? `（拟变更为${HEALTH_META[r.proposed_health]?.label || r.proposed_health}）` : ""}</p>` : ""}
        ${mode === "open" && state.user.role === "gardener" ? gardenerActions : ""}
        ${mode === "review" && state.user.role === "supervisor" ? supervisorActions : ""}
        ${mode === "review" && state.user.role === "gardener"
          ? '<p class="hint">等待主管复核中…</p>' : ""}
      </div>`;
  }

  // ------------------------------------------------------------- 我的认养
  async function loadMine() {
    const list = await get("/api/adoptions/mine");
    if (!list.length) {
      $("#mine-body").innerHTML = '<div class="empty">还没有认养记录，去地图选一棵树吧。</div>';
      return;
    }
    $("#mine-body").innerHTML = list.map((a) => `
      <div class="adopt-card" data-id="${a.id}">
        <div class="row">
          <div><b>${a.species}</b> <span class="tid">${a.tree_id}</span>
            <span class="pill ${a.status}" style="margin-left:8px">${a.status_label}</span></div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            ${a.status === "pending" ? `<button class="btn small" data-pay="${a.id}">立即支付</button>` : ""}
            ${a.status === "active" || a.status === "exited" ? `
              <button class="btn ghost small" data-report-btn="${a.id}">年度反馈</button>` : ""}
            ${a.status === "active" ? `
              <button class="btn danger small" data-exit="${a.id}">退出认养</button>` : ""}
          </div>
        </div>
        <div class="kv">${a.start_date} ~ ${a.end_date}｜${a.care_mode_label}
          ${a.hold_expires ? "｜支付截止 " + a.hold_expires.replace("T", " ").slice(0, 16) : ""}</div>
        ${a.payments.length ? `<div class="kv">模拟支付：${a.payments.map((p) =>
          `${p.id}（${(p.amount / 100).toFixed(2)} 元 · ${p.status === "paid" ? "已支付" : "已退款"}）`).join("，")}</div>` : ""}
        <div class="report-slot"></div>
      </div>`).join("");

    $$("[data-pay]").forEach((b) => (b.onclick = () => pay(b.dataset.pay).then(loadMine)));
    $$("[data-exit]").forEach((b) => (b.onclick = () => exitAdopt(b.dataset.exit).then(loadMine)));
    $$("[data-report-btn]").forEach((b) => (b.onclick = async () => {
      const card = b.closest(".adopt-card");
      const year = Number(prompt("反馈年份：", new Date().getFullYear()));
      if (!year) return;
      try {
        const r = await post(`/api/adoptions/${card.dataset.id}/annual-report`, { year });
        card.querySelector(".report-slot").innerHTML = `
          <div class="report-box">
            <b>🌳 ${r.year} 年度反馈</b>
            <div class="report-stats">
              <div><b>${r.water_done}/${r.water_total}</b>浇水</div>
              <div><b>${r.prune_done}/${r.prune_total}</b>修剪</div>
              <div><b>${r.pest_done}/${r.pest_total}</b>病虫观察</div>
              <div><b>${r.overdue_cnt}</b>逾期/迟做</div>
              <div><b>${r.false_alarm_cnt}</b>误报次数</div>
            </div>
            <p style="margin:6px 0 0">${esc(r.summary)}</p>
          </div>`;
      } catch (e) { toast(e.message, true); }
    }));
  }

  // ------------------------------------------------------------- 通知
  async function loadNotifications(reset = false) {
    const list = await get("/api/notifications");
    $("#bell-count").textContent = list.length;
    $("#bell-count").classList.toggle("hidden", list.length === 0);
    $("#notif-panel").innerHTML = list.length
      ? list.map((n) => `
          <div class="notif-item">
            <h4>${esc(n.title)}</h4><p>${esc(n.body)}</p>
            <time>${(n.created_at || "").replace("T", " ").slice(0, 16)} · 模拟短信</time>
          </div>`).join("")
      : '<p class="empty">暂无通知</p>';
  }
  $("#bell").onclick = (e) => {
    e.stopPropagation();
    $("#notif-panel").classList.toggle("hidden");
  };
  document.addEventListener("click", (e) => {
    if (!$("#notif-panel").contains(e.target)) $("#notif-panel").classList.add("hidden");
  });

  // ------------------------------------------------------------- 全局刷新
  async function refreshAll() {
    await loadMap();
    if (state.detailId) {
      const stillThere = state.trees.some((t) => t.id === state.detailId);
      if (stillThere) await loadDetail(state.detailId);
    }
    if ($("#view-calendar").classList.contains("active")) loadCalendar();
    if ($("#view-review").classList.contains("active")) loadReview();
    if ($("#view-mine").classList.contains("active")) loadMine();
    loadNotifications();
  }

  // 每 20 秒轮询一次：占位到期、状态变更、新通知
  setInterval(() => {
    loadMap();
    loadNotifications();
  }, 20000);

  (async function boot() {
    await initUsers();
    await refreshAll();
  })();
})();
