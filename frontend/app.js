const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");

function esc(v) {
  return String(v).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function lockCell(r) {
  if (r.lock_status === "locked") {
    return `<span class="locked">已锁${r.lock_confirmed_by ? `（确认人 ${esc(r.lock_confirmed_by)}）` : ""}</span>`;
  }
  if (r.lock_status === "pending") {
    if (role === "reader") {
      return `<span class="pending">待确认</span>
        <input class="confirm-code" data-id="${r.id}" inputmode="numeric" maxlength="6" placeholder="确认口令" />
        <button class="confirm-btn" data-id="${r.id}">确认锁定</button>`;
    }
    return `<span class="pending">待旁观账号确认</span>`;
  }
  if (r.level === "报警") return `<span class="alarm">报警行不可锁</span>`;
  if (role === "writer") {
    return `<button class="lock-btn" data-id="${r.id}">申请锁定</button>`;
  }
  return "未锁定";
}

function actionCell(r) {
  if (role !== "writer") return "";
  if (r.lock_status === "locked") return `<span class="locked">禁止改正</span>`;
  return `<input class="fix-ch4" data-id="${r.id}" type="number" step="0.01" value="${r.ch4_pct}" />
    <button class="fix-btn" data-id="${r.id}">改正浓度</button>`;
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) => `<tr class="${r.lock_status === "locked" ? "row-locked" : ""}">
        <td>${esc(r.site)}</td>
        <td>${r.ch4_pct}</td>
        <td class="${r.level === "报警" ? "alarm" : "ok"}">${r.level}</td>
        <td>${esc(r.note)}</td>
        <td>${lockCell(r)}</td>
        <td>${actionCell(r)}</td>
      </tr>`,
    )
    .join("");
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "旁观账号";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  connect();
  load();
}

async function load() {
  paint(await api("/api/readings"));
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.event === "locked") live.textContent = `${msg.site} 已双人确认锁定`;
    else if (msg.event === "lock_requested") live.textContent = "有一行正在等待旁观账号确认";
    else if (msg.site) live.textContent = `刚推送：${msg.site} ${msg.level}`;
    load();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
      }),
    });
    live.textContent = "上报成功";
  } catch (err) {
    live.textContent = err.message;
  }
};

rows.addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const id = btn.dataset.id;
  try {
    if (btn.classList.contains("lock-btn")) {
      const res = await api(`/api/readings/${id}/lock-request`, { method: "POST" });
      live.innerHTML = `锁定申请已提交，请旁观账号在另一会话输入确认口令：<strong class="pending">${esc(res.code)}</strong>`;
    } else if (btn.classList.contains("confirm-btn")) {
      const input = rows.querySelector(`.confirm-code[data-id="${id}"]`);
      await api(`/api/readings/${id}/lock-confirm`, {
        method: "POST",
        body: JSON.stringify({ code: input.value }),
      });
      live.textContent = "确认成功，该行已锁死";
    } else if (btn.classList.contains("fix-btn")) {
      const input = rows.querySelector(`.fix-ch4[data-id="${id}"]`);
      await api(`/api/readings/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ ch4_pct: Number(input.value) }),
      });
      live.textContent = "浓度已改正";
    }
    await load();
  } catch (err) {
    live.textContent = err.message;
  }
});

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
