const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let username = localStorage.getItem("methane_user") || "";

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const pendingBox = document.querySelector("#pending-box");
const pendingList = document.querySelector("#pending-list");

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function paint(list) {
  rows.innerHTML = list
    .map((r) => {
      const locked = !!r.locked;
      const alarm = r.level === "报警";
      const ch4Cell =
        role === "writer" && !locked
          ? `${esc(r.ch4_pct)}
            <form class="rowform" data-id="${r.id}">
              <input type="number" step="0.01" name="ch4" placeholder="改正为" />
              <button>改正</button>
            </form><span class="err" id="err-${r.id}"></span>`
          : esc(r.ch4_pct);
      let action;
      if (locked) {
        action = `<span class="lockbadge">🔒 已锁</span>`;
      } else if (alarm) {
        action = `<span>报警行不可锁</span>`;
      } else if (role === "writer") {
        action = r.lock_request_id
          ? `<span class="pending">待旁观确认</span>`
          : `<button data-act="lock" data-id="${r.id}">申请锁定</button>`;
      } else {
        action = r.lock_request_id ? `<span class="pending">待确认</span>` : "—";
      }
      return `<tr class="${locked ? "locked-row" : ""}">
        <td>${esc(r.site)}</td>
        <td>${ch4Cell}</td>
        <td class="${alarm ? "alarm" : "ok"}">${esc(r.level)}</td>
        <td>${esc(r.note)}</td>
        <td>${action}</td>
      </tr>`;
    })
    .join("");
}

function paintPending(list) {
  if (role !== "reader") {
    pendingBox.hidden = true;
    return;
  }
  pendingBox.hidden = false;
  if (list.length === 0) {
    pendingList.textContent = "暂无待确认申请。";
    return;
  }
  pendingList.innerHTML = list
    .map(
      (p) => `<div class="pend-item" data-pid="${p.id}">
        检查员 ${esc(p.requested_by)} 申请锁定「${esc(p.site)}」（${esc(p.ch4_pct)}%，正常行）
        <input type="password" class="code" placeholder="确认口令" />
        <button data-act="confirm" data-id="${p.id}">确认锁定</button>
        <span class="err" id="perr-${p.id}"></span>
      </div>`,
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
  document.querySelector("#who").textContent =
    (role === "writer" ? "检查员 " : "旁观 ") + username;
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  connect();
  load();
}

async function load() {
  const list = await api("/api/readings");
  paint(list);
  if (role === "reader") {
    paintPending(await api("/api/locks/pending"));
  } else {
    paintPending([]);
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const event = JSON.parse(ev.data);
    const label = {
      reading_created: `新记录：${event.site} ${event.level}`,
      reading_corrected: `浓度已改正：${event.site} → ${event.ch4_pct}%`,
      lock_requested: `检查员申请锁定：${event.site}，等待旁观确认`,
      lock_confirmed: `已锁定：${event.site}`,
    }[event.type] || "数据有更新";
    live.textContent = `刚推送：${label}`;
    load();
  };
}

document.querySelector("#go").onclick = async () => {
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: document.querySelector("#user").value,
        password: document.querySelector("#pass").value,
      }),
    });
    token = data.access_token;
    role = data.role;
    username = data.username;
    localStorage.setItem(tokenKey, token);
    localStorage.setItem("methane_role", role);
    localStorage.setItem("methane_user", username);
    showApp();
  } catch (err) {
    live.textContent = err.message;
  }
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
    form.reset();
  } catch (err) {
    live.textContent = err.message;
  }
};

rows.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act='lock']");
  if (!btn) return;
  btn.disabled = true;
  try {
    await api(`/api/readings/${btn.dataset.id}/lock-requests`, { method: "POST" });
  } catch (err) {
    live.textContent = err.message;
    btn.disabled = false;
  }
});

rows.addEventListener("submit", async (e) => {
  const rf = e.target.closest("form.rowform");
  if (!rf) return;
  e.preventDefault();
  const id = rf.dataset.id;
  const errBox = document.querySelector(`#err-${id}`);
  errBox.textContent = "";
  try {
    await api(`/api/readings/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ ch4_pct: Number(rf.elements.ch4.value) }),
    });
  } catch (err) {
    errBox.textContent = err.message;
  }
});

pendingList.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act='confirm']");
  if (!btn) return;
  const id = btn.dataset.id;
  const item = pendingList.querySelector(`.pend-item[data-pid="${id}"]`);
  const code = item.querySelector(".code").value;
  const errBox = document.querySelector(`#perr-${id}`);
  errBox.textContent = "";
  btn.disabled = true;
  try {
    await api(`/api/locks/${id}/confirm`, {
      method: "POST",
      body: JSON.stringify({ code }),
    });
  } catch (err) {
    errBox.textContent = err.message;
    btn.disabled = false;
  }
});

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();
