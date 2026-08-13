const content = document.querySelector("#adminContent");
const pageTitle = document.querySelector("#pageTitle");
const detailDialog = document.querySelector("#detailDialog");
const detailContent = document.querySelector("#detailContent");
const imageDialog = document.querySelector("#imageDialog");
const previewImage = document.querySelector("#previewImage");
const state = { page: 1, pageSize: 20, search: "" };

const escapeHtml = (value) => String(value ?? "—").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
const dateText = (value) => value ? new Intl.DateTimeFormat("zh-CN", {dateStyle:"medium",timeStyle:"short"}).format(new Date(value)) : "—";
const moneyText = (value) => `¥${(Number(value || 0) / 100).toFixed(2)}`;
const route = location.pathname.split("/")[2] || "home";
const routeNames = {home:"数据概览",users:"用户管理",jobs:"生成记录",payments:"充值记录"};

async function api(path, options = {}) {
  const response = await fetch(path, {credentials:"same-origin",cache:"no-store",...options});
  if (response.status === 401) { location.replace("/admin/login"); throw new Error("登录已失效"); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "数据加载失败");
  return data;
}

function statusBadge(status) {
  const labels = {active:"正常",disabled:"已停用",completed:"已完成",failed:"失败",running:"生成中",queued:"排队中",paid:"已支付",pending:"待支付",processing:"处理中",consumed:"已使用"};
  const type = ["active","completed","paid","consumed"].includes(status) ? "success" : (["disabled","failed"].includes(status) ? "failed" : "pending");
  return `<span class="badge ${type}">${escapeHtml(labels[status] || status)}</span>`;
}

function pager(data, load) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  const wrap = document.createElement("div");
  wrap.className = "pagination";
  wrap.innerHTML = `<span>共 ${data.total} 条 · 第 ${data.page}/${pages} 页</span><button ${data.page <= 1 ? "disabled" : ""}>上一页</button><button ${data.page >= pages ? "disabled" : ""}>下一页</button>`;
  const buttons = wrap.querySelectorAll("button");
  buttons[0].onclick = () => { state.page -= 1; load(); };
  buttons[1].onclick = () => { state.page += 1; load(); };
  return wrap;
}

function tablePanel(title, subtitle, headers, rows) {
  return `<div class="panel"><div class="panel-heading"><h2>${escapeHtml(title)}</h2><span>${escapeHtml(subtitle)}</span></div><div class="table-wrap"><table><thead><tr>${headers.map((item) => `<th>${item}</th>`).join("")}</tr></thead><tbody>${rows || `<tr><td colspan="${headers.length}" class="empty">暂无数据</td></tr>`}</tbody></table></div></div>`;
}

async function loadHome() {
  content.innerHTML = `<div class="loading">正在汇总数据…</div>`;
  const [users, jobs, payments] = await Promise.all([
    api("/admin/api/users?page=1&page_size=1"), api("/admin/api/jobs?page=1&page_size=5"), api("/admin/api/payments?page=1&page_size=5"),
  ]);
  content.innerHTML = `<div class="stats-grid">
    <div class="stat-card"><span>注册用户</span><strong>${users.total}</strong><small>全部微信 OpenID 用户</small></div>
    <div class="stat-card"><span>生成任务</span><strong>${jobs.total}</strong><small>文件系统任务总数</small></div>
    <div class="stat-card"><span>充值订单</span><strong>${payments.total}</strong><small>包含所有支付状态</small></div>
  </div>` + tablePanel("最近生成任务", "最新 5 条", ["用户","商品类型","状态","进度","生成时间"], jobs.items.map((item) => `<tr><td class="mono">${escapeHtml(item.openid)}</td><td>${escapeHtml(item.product?.subcategory)}</td><td>${statusBadge(item.status)}</td><td>${item.progress}%</td><td>${dateText(item.created_at)}</td></tr>`).join(""));
}

async function loadUsers() {
  content.innerHTML = `<div class="loading">正在加载用户…</div>`;
  const query = new URLSearchParams({page:state.page,page_size:state.pageSize,search:state.search});
  const data = await api(`/admin/api/users?${query}`);
  content.innerHTML = `<div class="panel"><div class="toolbar"><strong>全部注册用户</strong><form id="searchForm"><input name="search" value="${escapeHtml(state.search)}" placeholder="搜索完整或部分 OpenID"><button>搜索</button></form></div><div class="table-wrap"><table><thead><tr><th>微信 OpenID</th><th>创建时间</th><th>最近登录</th><th>当前余额</th><th>剩余次数</th><th>状态</th><th>操作</th></tr></thead><tbody>${data.items.map((user) => `<tr><td class="mono">${escapeHtml(user.openid)}</td><td>${dateText(user.created_at)}</td><td>${dateText(user.last_login_at)}</td><td class="money">${moneyText(user.balance_cent)}</td><td>${user.remaining_uses}</td><td>${statusBadge(user.status)}</td><td><button class="text-button" data-user="${user.id}">查看详情</button></td></tr>`).join("") || `<tr><td colspan="7" class="empty">暂无用户</td></tr>`}</tbody></table></div></div>`;
  content.querySelector(".panel").append(pager(data, loadUsers));
  document.querySelector("#searchForm").onsubmit = (event) => { event.preventDefault(); state.search = new FormData(event.currentTarget).get("search").trim(); state.page = 1; loadUsers(); };
  content.querySelectorAll("[data-user]").forEach((button) => button.onclick = () => showUser(button.dataset.user));
}

function jobCards(items) {
  return items.map((job) => `<article class="job-card"><div class="job-meta"><div><strong>${escapeHtml(job.product?.product_name || job.product?.subcategory)}</strong><small><span class="mono">${escapeHtml(job.job_id)}</span> · ${escapeHtml(job.openid || "")}</small><small>${dateText(job.created_at)} · ${escapeHtml(job.stage)}</small></div><div>${statusBadge(job.status)}　${job.progress}% ${job.download_url ? `　<a class="download-link" href="${job.download_url}">下载 ZIP</a>` : ""}</div></div>${job.images?.length ? `<div class="job-images">${job.images.map((image) => `<button data-image="${image.url}" title="${escapeHtml(image.label)}"><img loading="lazy" src="${image.url}" alt="${escapeHtml(image.label)}"></button>`).join("")}</div>` : ""}</article>`).join("");
}

async function loadJobs() {
  content.innerHTML = `<div class="loading">正在读取生成记录…</div>`;
  const data = await api(`/admin/api/jobs?page=${state.page}&page_size=${state.pageSize}`);
  content.innerHTML = `<div class="panel"><div class="panel-heading"><h2>全部生成任务</h2><span>展示图、商品长图与 ZIP 均使用管理员专属鉴权</span></div>${jobCards(data.items) || `<div class="empty">暂无生成记录</div>`}</div>`;
  content.querySelector(".panel").append(pager(data, loadJobs));
  bindImagePreview(content);
}

async function loadPayments() {
  content.innerHTML = `<div class="loading">正在加载充值记录…</div>`;
  const data = await api(`/admin/api/payments?page=${state.page}&page_size=${state.pageSize}`);
  const rows = data.items.map((item) => `<tr><td class="mono">${escapeHtml(item.order_id)}</td><td class="mono">${escapeHtml(item.openid)}</td><td>${escapeHtml(item.package_name || item.package_id)}</td><td>${item.credits ?? "—"}</td><td class="money">${moneyText(item.total_fee)}</td><td>${statusBadge(item.order_status || (item.pay_status ? "paid" : "pending"))}</td><td>${dateText(item.create_time)}</td><td>${dateText(item.pay_time)}</td></tr>`).join("");
  content.innerHTML = tablePanel("全部充值订单", "只读查看，不影响现有充值逻辑", ["订单号","微信 OpenID","套餐","次数","金额","支付状态","创建时间","支付时间"], rows);
  content.querySelector(".panel").append(pager(data, loadPayments));
}

async function showUser(userId) {
  detailContent.innerHTML = `<div class="loading">正在加载用户详情…</div>`;
  detailDialog.showModal();
  try {
    const data = await api(`/admin/api/users/${userId}`);
    const user = data.user;
    const payments = data.recent_payments.map((item) => `<tr><td class="mono">${escapeHtml(item.order_id)}</td><td>${escapeHtml(item.package_name || item.package_id)}</td><td>${moneyText(item.total_fee)}</td><td>${statusBadge(item.order_status || (item.pay_status ? "paid" : "pending"))}</td><td>${dateText(item.pay_time || item.create_time)}</td></tr>`).join("");
    detailContent.innerHTML = `<h2>用户详细信息</h2><div class="detail-grid"><div class="detail-field"><span>微信 OpenID</span><strong class="mono">${escapeHtml(user.openid)}</strong></div><div class="detail-field"><span>用户状态</span><strong>${statusBadge(user.status)}</strong></div><div class="detail-field"><span>当前余额 / 剩余次数</span><strong>${moneyText(user.balance_cent)} / ${user.remaining_uses} 次</strong></div><div class="detail-field"><span>用户 ID</span><strong>${user.id}</strong></div><div class="detail-field"><span>创建时间</span><strong>${dateText(user.created_at)}</strong></div><div class="detail-field"><span>最近登录</span><strong>${dateText(user.last_login_at)}</strong></div></div><section class="detail-section"><h3>最近生成记录</h3><div class="panel">${jobCards(data.recent_jobs) || `<div class="empty">暂无生成记录</div>`}</div></section><section class="detail-section">${tablePanel("最近充值记录", `共 ${data.payment_total} 条`, ["订单号","套餐","金额","状态","时间"], payments)}</section>`;
    bindImagePreview(detailContent);
  } catch (error) { detailContent.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
}

function bindImagePreview(root) {
  root.querySelectorAll("[data-image]").forEach((button) => button.onclick = () => { previewImage.src = button.dataset.image; imageDialog.showModal(); });
}

document.querySelectorAll(".dialog-close").forEach((button) => button.onclick = () => button.closest("dialog").close());
document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); }));
document.querySelector("#logoutButton").onclick = async () => { await api("/admin/api/logout", {method:"POST"}); location.replace("/admin/login"); };

async function initialize() {
  document.querySelectorAll("[data-route]").forEach((link) => link.classList.toggle("active", link.dataset.route === route));
  pageTitle.textContent = routeNames[route] || "管理后台";
  try {
    const session = await api("/admin/api/session");
    document.querySelector("#adminName").textContent = session.username;
    ({home:loadHome,users:loadUsers,jobs:loadJobs,payments:loadPayments}[route] || loadHome)();
  } catch (error) { content.innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`; }
}

initialize();
