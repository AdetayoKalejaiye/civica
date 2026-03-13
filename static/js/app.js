// ─── STATE ────────────────────────────────────────────────────────────────────

const STATE = {
  location: localStorage.getItem("civic_location") || "National",
  state: localStorage.getItem("civic_state") || null,
  darkMode: localStorage.getItem("civic_dark") === "true",
  updates: JSON.parse(localStorage.getItem("civic_updates") || "[]"),
  subscription: null,
  online: navigator.onLine,
};

// ─── DB (IndexedDB for offline) ───────────────────────────────────────────────

let db;
function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("civicbot", 1);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains("updates")) {
        db.createObjectStore("updates", { keyPath: "id" });
      }
    };
    req.onsuccess = (e) => resolve(e.target.result);
    req.onerror = reject;
  });
}

async function saveUpdatesOffline(updates) {
  const d = await openDB();
  const tx = d.transaction("updates", "readwrite");
  const store = tx.objectStore("updates");
  for (const u of updates) store.put(u);
}

async function getOfflineUpdates() {
  const d = await openDB();
  return new Promise((resolve) => {
    const tx = d.transaction("updates", "readonly");
    const store = tx.objectStore("updates");
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result.sort((a, b) => new Date(b.created_at) - new Date(a.created_at)));
    req.onerror = () => resolve([]);
  });
}

// ─── SERVICE WORKER & PUSH ────────────────────────────────────────────────────

async function registerSW() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.register("/sw.js");
    console.log("SW registered");
    return reg;
  } catch (e) {
    console.error("SW registration failed:", e);
  }
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
}

async function subscribeToPush(reg) {
  try {
    const vapidKey = document.getElementById("vapid-key")?.dataset.key;
    if (!vapidKey) return null;

    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidKey),
    });

    await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription: sub.toJSON(),
        location: STATE.location,
        state: STATE.state,
      }),
    });

    STATE.subscription = sub;
    return sub;
  } catch (e) {
    console.error("Push subscription failed:", e);
    return null;
  }
}

async function requestNotifications() {
  if (!("Notification" in window)) return false;
  if (Notification.permission === "granted") return true;
  const perm = await Notification.requestPermission();
  return perm === "granted";
}

// ─── DATA FETCHING ────────────────────────────────────────────────────────────

async function fetchUpdates() {
  updateConnectionStatus();
  try {
    const res = await fetch(`/api/updates?location=${encodeURIComponent(STATE.location)}`);
    if (!res.ok) throw new Error("Network error");
    const updates = await res.json();
    await saveUpdatesOffline(updates);
    localStorage.setItem("civic_updates", JSON.stringify(updates));
    return updates;
  } catch (e) {
    console.warn("Offline — loading cached updates");
    return await getOfflineUpdates();
  }
}

// ─── RENDER ───────────────────────────────────────────────────────────────────

function formatDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function categoryColor(cat) {
  const map = { National: "#B22234", Local: "#3C3B6E", Policy: "#c17f24", Election: "#2a6db5" };
  return map[cat] || "#B22234";
}

function renderUpdate(update) {
  const { content, created_at, location } = update;
  const stories = content.stories || [];

  return `
    <article class="update-card" data-id="${update.id}">
      <div class="update-meta">
        <span class="update-location">📍 ${location}</span>
        <span class="update-date">${formatDate(created_at)}</span>
      </div>
      <h2 class="update-headline">${content.headline}</h2>
      <p class="update-summary">${content.summary}</p>
      <div class="stories-grid">
        ${stories.map(s => `
          <div class="story-card">
            <div class="story-category" style="background:${categoryColor(s.category)}">${s.category}</div>
            <h3 class="story-title">${s.title}</h3>
            <p class="story-body">${s.body}</p>
            <span class="story-source">— ${s.source}</span>
          </div>
        `).join("")}
      </div>
      ${content.civic_fact ? `
        <div class="civic-fact">
          <span class="fact-label">💡 Did You Know?</span>
          <p>${content.civic_fact}</p>
        </div>
      ` : ""}
    </article>
  `;
}

function renderGenerating() {
  return `
    <div class="empty-state">
      <div class="empty-icon">⏳</div>
      <h3>Generating your first digest...</h3>
      <p>Fetching today's civic news. This takes about 10 seconds.</p>
    </div>
  `;
}

function renderEmpty() {
  return `
    <div class="empty-state">
      <div class="empty-icon">🗳️</div>
      <h3>No updates yet</h3>
      <p>Your first civic digest will arrive within 48 hours.<br>Check back soon.</p>
    </div>
  `;
}

function renderSkeleton() {
  return Array(3).fill(`
    <div class="update-card skeleton">
      <div class="skeleton-line short"></div>
      <div class="skeleton-line long"></div>
      <div class="skeleton-line medium"></div>
      <div class="skeleton-grid">
        <div class="skeleton-box"></div>
        <div class="skeleton-box"></div>
      </div>
    </div>
  `).join("");
}

async function renderUpdates() {
  const feed = document.getElementById("feed");
  feed.innerHTML = renderSkeleton();

  const updates = await fetchUpdates();

  if (!updates || updates.length === 0) {
    // Auto-generate first digest
    feed.innerHTML = renderGenerating();
    try {
      const res = await fetch("/api/generate-digest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location: STATE.location, state: STATE.state }),
      });
      if (res.ok) {
        const fresh = await res.json();
        await saveUpdatesOffline([fresh]);
        feed.innerHTML = renderUpdate(fresh);
        return;
      }
    } catch (e) {
      console.error("Auto-generate failed:", e);
    }
    feed.innerHTML = renderEmpty();
    return;
  }

  feed.innerHTML = updates.map(renderUpdate).join("");
}


// ─── LOCATION MODAL ───────────────────────────────────────────────────────────

const US_STATES = [
  ["AL","Alabama"],["AK","Alaska"],["AZ","Arizona"],["AR","Arkansas"],["CA","California"],
  ["CO","Colorado"],["CT","Connecticut"],["DE","Delaware"],["FL","Florida"],["GA","Georgia"],
  ["HI","Hawaii"],["ID","Idaho"],["IL","Illinois"],["IN","Indiana"],["IA","Iowa"],
  ["KS","Kansas"],["KY","Kentucky"],["LA","Louisiana"],["ME","Maine"],["MD","Maryland"],
  ["MA","Massachusetts"],["MI","Michigan"],["MN","Minnesota"],["MS","Mississippi"],["MO","Missouri"],
  ["MT","Montana"],["NE","Nebraska"],["NV","Nevada"],["NH","New Hampshire"],["NJ","New Jersey"],
  ["NM","New Mexico"],["NY","New York"],["NC","North Carolina"],["ND","North Dakota"],["OH","Ohio"],
  ["OK","Oklahoma"],["OR","Oregon"],["PA","Pennsylvania"],["RI","Rhode Island"],["SC","South Carolina"],
  ["SD","South Dakota"],["TN","Tennessee"],["TX","Texas"],["UT","Utah"],["VT","Vermont"],
  ["VA","Virginia"],["WA","Washington"],["WV","West Virginia"],["WI","Wisconsin"],["WY","Wyoming"]
];

function openLocationModal() {
  const modal = document.getElementById("location-modal");
  const stateSelect = document.getElementById("state-select");
  const cityInput = document.getElementById("city-input");

  stateSelect.innerHTML = `<option value="">— State (optional) —</option>` +
    US_STATES.map(([code, name]) => `<option value="${code}" ${STATE.state === code ? "selected" : ""}>${name}</option>`).join("");

  cityInput.value = STATE.location !== "National" ? STATE.location.split(",")[0] : "";
  modal.classList.add("open");
}

function closeLocationModal() {
  document.getElementById("location-modal").classList.remove("open");
}

async function saveLocation() {
  const city = document.getElementById("city-input").value.trim();
  const stateCode = document.getElementById("state-select").value;
  const stateName = US_STATES.find(([c]) => c === stateCode)?.[1] || null;

  if (city && stateCode) {
    STATE.location = `${city}, ${stateCode}`;
    STATE.state = stateCode;
  } else if (stateCode) {
    STATE.location = stateName;
    STATE.state = stateCode;
  } else {
    STATE.location = "National";
    STATE.state = null;
  }

  localStorage.setItem("civic_location", STATE.location);
  localStorage.setItem("civic_state", STATE.state || "");
  document.getElementById("current-location").textContent = STATE.location;

  // Update subscription location
  if (STATE.subscription) {
    await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription: STATE.subscription.toJSON(),
        location: STATE.location,
        state: STATE.state,
      }),
    });
  }

  closeLocationModal();
  renderUpdates();
}

// ─── DARK MODE ────────────────────────────────────────────────────────────────

function applyDarkMode(dark) {
  document.documentElement.classList.toggle("dark", dark);
  document.getElementById("dark-toggle").textContent = dark ? "☀️" : "🌙";
  STATE.darkMode = dark;
  localStorage.setItem("civic_dark", dark);
}

function toggleDark() {
  applyDarkMode(!STATE.darkMode);
}

// ─── CONNECTION STATUS ────────────────────────────────────────────────────────

function updateConnectionStatus() {
  const banner = document.getElementById("offline-banner");
  STATE.online = navigator.onLine;
  banner.classList.toggle("hidden", STATE.online);
}

// ─── NOTIFICATIONS BUTTON ─────────────────────────────────────────────────────

async function handleNotifyButton() {
  const btn = document.getElementById("notify-btn");
  const granted = await requestNotifications();
  if (!granted) {
    btn.textContent = "Notifications blocked";
    btn.disabled = true;
    return;
  }
  const reg = await navigator.serviceWorker.ready;
  const sub = await subscribeToPush(reg);
  if (sub) {
    btn.textContent = "✅ Notifications on";
    btn.classList.add("active");
    btn.disabled = true;
  }
}

// ─── Q&A ──────────────────────────────────────────────────────────────────────

const MAX_QUESTIONS = 5;

function getQAState() {
  const today = new Date().toDateString();
  const stored = JSON.parse(localStorage.getItem("civic_qa") || "{}");
  if (stored.date !== today) return { date: today, count: 0, history: [] };
  return stored;
}

function saveQAState(state) {
  localStorage.setItem("civic_qa", JSON.stringify(state));
}

function updateQACounter() {
  const qa = getQAState();
  const remaining = MAX_QUESTIONS - qa.count;
  document.getElementById("qa-counter").textContent = `${remaining}/${MAX_QUESTIONS} questions left today`;
  document.getElementById("qa-input").disabled = remaining <= 0;
  document.getElementById("qa-submit").disabled = remaining <= 0;
  if (remaining <= 0) {
    document.getElementById("qa-input").placeholder = "Daily limit reached. Come back tomorrow.";
  }
}

function appendQAMessage(role, text) {
  const log = document.getElementById("qa-log");
  const msg = document.createElement("div");
  msg.className = `qa-msg qa-${role}`;
  msg.innerHTML = `<span class="qa-role">${role === "user" ? "You" : "CivicBot"}</span><p>${text}</p>`;
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
}

function renderQAHistory() {
  const qa = getQAState();
  const log = document.getElementById("qa-log");
  log.innerHTML = "";
  for (const msg of qa.history) {
    appendQAMessage(msg.role, msg.text);
  }
}

async function submitQuestion() {
  const input = document.getElementById("qa-input");
  const question = input.value.trim();
  if (!question) return;

  const qa = getQAState();
  if (qa.count >= MAX_QUESTIONS) return;

  input.value = "";
  appendQAMessage("user", question);

  // Show typing indicator
  const log = document.getElementById("qa-log");
  const typing = document.createElement("div");
  typing.className = "qa-msg qa-bot qa-typing";
  typing.innerHTML = `<span class="qa-role">CivicBot</span><p>⏳ Thinking...</p>`;
  log.appendChild(typing);
  log.scrollTop = log.scrollHeight;

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, location: STATE.location }),
    });
    const data = await res.json();
    typing.remove();

    let answer;
    if (data.not_civic) {
      answer = "❌ I can only answer civic and political questions — things like elections, legislation, government processes, voting rights, and public policy.";
    } else {
      answer = data.answer || "Sorry, I couldn't find a clear answer. Try rephrasing.";
      // Count only real civic questions
      qa.count += 1;
    }

    appendQAMessage("bot", answer);
    qa.history.push({ role: "user", text: question }, { role: "bot", text: answer });
    if (qa.history.length > 20) qa.history = qa.history.slice(-20);
    saveQAState(qa);
    updateQACounter();

  } catch (e) {
    typing.remove();
    appendQAMessage("bot", "Sorry, something went wrong. Please try again.");
  }
}

// ─── INIT ─────────────────────────────────────────────────────────────────────

async function init() {
  // Dark mode
  applyDarkMode(STATE.darkMode);

  // Location display
  document.getElementById("current-location").textContent = STATE.location;

  // Register SW
  const reg = await registerSW();

  // Check existing push subscription
  if (reg) {
    const existing = await reg.pushManager.getSubscription();
    if (existing) {
      STATE.subscription = existing;
      const btn = document.getElementById("notify-btn");
      btn.textContent = "Notifications on";
      btn.classList.add("active");
      btn.disabled = true;
    }
  }

  // Network listeners
  window.addEventListener("online", updateConnectionStatus);
  window.addEventListener("offline", updateConnectionStatus);

  // Event listeners
  document.getElementById("dark-toggle").addEventListener("click", toggleDark);
  document.getElementById("location-btn").addEventListener("click", openLocationModal);
  document.getElementById("notify-btn").addEventListener("click", handleNotifyButton);
  document.getElementById("save-location").addEventListener("click", saveLocation);
  document.getElementById("close-modal").addEventListener("click", closeLocationModal);
  document.getElementById("location-modal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeLocationModal();
  });

  // Q&A
  renderQAHistory();
  updateQACounter();
  document.getElementById("qa-submit").addEventListener("click", submitQuestion);
  document.getElementById("qa-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitQuestion(); }
  });

  // Load updates
  renderUpdates();
}

document.addEventListener("DOMContentLoaded", init);
