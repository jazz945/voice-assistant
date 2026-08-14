/* MotoMatch — client SPA sans dépendance. */

const state = {
  token: localStorage.getItem("motomatch_token"),
  userId: Number(localStorage.getItem("motomatch_user_id")) || null,
  meta: { bike_categories: [], riding_styles: [], pace_levels: [] },
  selectedStyles: new Set(),
  activeMatch: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

// --- Accès API --------------------------------------------------------------

async function api(path, { method = "GET", body } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (response.status === 401) {
    signOut();
    throw new Error("Session expirée, reconnecte-toi.");
  }
  if (response.status === 204) return null;

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(payload));
  return payload;
}

function errorMessage(payload) {
  const detail = payload.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => `${(item.loc || []).slice(1).join(".")} : ${item.msg}`).join(" · ");
  }
  return "Une erreur est survenue.";
}

let toastTimer;
function toast(message, isError = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", isError);
  element.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.add("hidden"), 3800);
}

// --- Navigation -------------------------------------------------------------

function showView(name) {
  $$(".view").forEach((view) => view.classList.add("hidden"));
  $(`#view-${name}`).classList.remove("hidden");
  $$(".nav-btn[data-view]").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.view === name),
  );
  if (name === "discover") loadDeck();
  if (name === "matches") loadMatches();
}

function signIn(session) {
  state.token = session.token;
  state.userId = session.user_id;
  localStorage.setItem("motomatch_token", session.token);
  localStorage.setItem("motomatch_user_id", String(session.user_id));
  $("#nav").classList.remove("hidden");
  showView(session.has_profile ? "discover" : "profile");
  if (!session.has_profile) toast("Complète ton profil moto pour commencer.");
}

function signOut() {
  state.token = null;
  state.userId = null;
  localStorage.removeItem("motomatch_token");
  localStorage.removeItem("motomatch_user_id");
  $("#nav").classList.add("hidden");
  $$(".view").forEach((view) => view.classList.add("hidden"));
  $("#view-auth").classList.remove("hidden");
}

// --- Authentification -------------------------------------------------------

async function authenticate(path) {
  const form = $("#auth-form");
  if (!form.reportValidity()) return;
  const data = Object.fromEntries(new FormData(form));
  try {
    const session = await api(path, { method: "POST", body: data });
    signIn(session);
    if (session.has_profile) await fillProfileForm();
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Formulaire de profil ---------------------------------------------------

function renderMeta() {
  const label = (value) => value.replaceAll("-", " ");

  $("#bike-category").innerHTML = state.meta.bike_categories
    .map((c) => `<option value="${c}">${label(c)}</option>`)
    .join("");
  $("#pace").innerHTML = state.meta.pace_levels
    .map((p) => `<option value="${p}">${label(p)}</option>`)
    .join("");
  $("#filter-category").innerHTML =
    '<option value="">Toutes</option>' +
    state.meta.bike_categories.map((c) => `<option value="${c}">${label(c)}</option>`).join("");
  $("#filter-style").innerHTML =
    '<option value="">Toutes</option>' +
    state.meta.riding_styles.map((s) => `<option value="${s}">${label(s)}</option>`).join("");

  $("#styles-choices").innerHTML = state.meta.riding_styles
    .map((s) => `<span class="chip" data-style="${s}">${label(s)}</span>`)
    .join("");
  $$("#styles-choices .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const style = chip.dataset.style;
      if (state.selectedStyles.has(style)) state.selectedStyles.delete(style);
      else state.selectedStyles.add(style);
      chip.classList.toggle("selected");
    });
  });
}

async function fillProfileForm() {
  const me = await api("/api/me");
  if (!me.profile) return;
  const form = $("#profile-form");
  Object.entries(me.profile).forEach(([key, value]) => {
    const field = form.elements[key];
    if (!field || value === null) return;
    if (field.type === "checkbox") field.checked = Boolean(value);
    else if (key !== "riding_styles") field.value = value;
  });
  state.selectedStyles = new Set(me.profile.riding_styles || []);
  $$("#styles-choices .chip").forEach((chip) =>
    chip.classList.toggle("selected", state.selectedStyles.has(chip.dataset.style)),
  );
}

async function saveProfile(event) {
  event.preventDefault();
  const form = event.target;
  const data = Object.fromEntries(new FormData(form));
  const numbers = [
    "birth_year",
    "latitude",
    "longitude",
    "bike_year",
    "engine_cc",
    "experience_years",
    "annual_km",
    "max_travel_km",
  ];
  numbers.forEach((key) => {
    data[key] = data[key] === "" || data[key] === undefined ? null : Number(data[key]);
  });
  ["experience_years", "annual_km", "max_travel_km"].forEach((key) => {
    if (data[key] === null) delete data[key];
  });
  data.has_passenger_seat = form.elements.has_passenger_seat.checked;
  data.riding_styles = Array.from(state.selectedStyles);

  try {
    await api("/api/me/profile", { method: "PUT", body: data });
    toast("Profil enregistré. Bonne route !");
    showView("discover");
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Découverte -------------------------------------------------------------

function buildDiscoverQuery() {
  const form = $("#filter-form");
  const params = new URLSearchParams();
  new FormData(form).forEach((value, key) => {
    if (String(value).trim() !== "") params.append(key, value);
  });
  return params.toString();
}

async function loadDeck() {
  const deck = $("#deck");
  deck.innerHTML = '<p class="empty">Recherche des motards autour de toi…</p>';
  try {
    const query = buildDiscoverQuery();
    const data = await api(`/api/discover${query ? `?${query}` : ""}`);
    renderDeck(data.results);
  } catch (error) {
    deck.innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

function renderDeck(results) {
  const deck = $("#deck");
  if (!results.length) {
    deck.innerHTML =
      '<p class="empty">Plus personne à afficher. Élargis le rayon ou reviens plus tard.</p>';
    return;
  }
  deck.innerHTML = results.map(renderRiderCard).join("");
  deck.querySelectorAll("[data-swipe]").forEach((button) => {
    button.addEventListener("click", () =>
      sendSwipe(Number(button.dataset.user), button.dataset.swipe),
    );
  });
}

function renderRiderCard(item) {
  const p = item.profile;
  const bikeYear = p.bike_year ? `${p.bike_year} · ` : "";
  const styles = (p.riding_styles || [])
    .map((s) => `<span class="chip static">${escapeHtml(s.replaceAll("-", " "))}</span>`)
    .join("");
  const notes = (item.highlights || [])
    .map((note) => `<li>${escapeHtml(note)}</li>`)
    .join("");

  return `
    <article class="rider">
      <div class="rider-head">
        <div>
          <h3>${escapeHtml(p.display_name)}, ${p.age}</h3>
          <p class="rider-sub">${escapeHtml(p.city)} · ${item.distance_km} km</p>
        </div>
        <div class="score">${Math.round(item.score)}<small>compat.</small></div>
      </div>
      <div class="bike">
        <strong>${escapeHtml(p.bike_brand)} ${escapeHtml(p.bike_model)}</strong>
        <div class="bike-meta">
          ${bikeYear}${p.engine_cc} cm³ · ${escapeHtml(p.bike_category)} ·
          rythme ${escapeHtml(p.pace.replaceAll("-", " "))} ·
          ${p.experience_years} an(s) de permis
        </div>
      </div>
      ${p.bio ? `<p class="bio">${escapeHtml(p.bio)}</p>` : ""}
      <div class="chips">${styles}</div>
      ${notes ? `<ul class="notes">${notes}</ul>` : ""}
      <div class="rider-actions">
        <button class="btn-pass" data-swipe="pass" data-user="${p.user_id}">Passer</button>
        <button class="btn-like" data-swipe="like" data-user="${p.user_id}">Rouler ensemble</button>
      </div>
    </article>`;
}

async function sendSwipe(targetUserId, direction) {
  try {
    const result = await api("/api/swipes", {
      method: "POST",
      body: { target_user_id: targetUserId, direction },
    });
    if (result.matched) toast("C'est un match ! Direction l'onglet Matchs.");
    await loadDeck();
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Matchs et messagerie ---------------------------------------------------

async function loadMatches() {
  const list = $("#match-list");
  try {
    const data = await api("/api/matches");
    if (!data.results.length) {
      list.innerHTML = '<li class="muted">Aucun match pour le moment.</li>';
      return;
    }
    list.innerHTML = data.results
      .map(
        (match) => `
        <li data-match="${match.match_id}" data-name="${escapeHtml(match.profile.display_name)}">
          <div class="match-name">${escapeHtml(match.profile.display_name)}</div>
          <div class="match-preview">${escapeHtml(
            match.last_message || `${match.profile.bike_brand} ${match.profile.bike_model}`,
          )}</div>
        </li>`,
      )
      .join("");
    list.querySelectorAll("li[data-match]").forEach((item) => {
      item.addEventListener("click", () => openChat(Number(item.dataset.match), item.dataset.name));
    });
    if (state.activeMatch) {
      const current = list.querySelector(`li[data-match="${state.activeMatch}"]`);
      if (current) current.classList.add("active");
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function openChat(matchId, name) {
  state.activeMatch = matchId;
  $("#chat-title").textContent = `Conversation avec ${name}`;
  $("#chat-form").classList.remove("hidden");
  $$("#match-list li").forEach((item) =>
    item.classList.toggle("active", Number(item.dataset.match) === matchId),
  );
  await refreshMessages();
}

async function refreshMessages() {
  if (!state.activeMatch) return;
  const container = $("#chat-messages");
  try {
    const data = await api(`/api/matches/${state.activeMatch}/messages`);
    container.innerHTML = data.results.length
      ? data.results
          .map(
            (message) =>
              `<div class="bubble ${message.sender_id === state.userId ? "mine" : "theirs"}">${escapeHtml(
                message.body,
              )}</div>`,
          )
          .join("")
      : '<p class="muted">Lance la conversation : demandez-vous où vous roulez ce week-end.</p>';
    container.scrollTop = container.scrollHeight;
  } catch (error) {
    toast(error.message, true);
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const input = event.target.elements.body;
  const body = input.value.trim();
  if (!body || !state.activeMatch) return;
  try {
    await api(`/api/matches/${state.activeMatch}/messages`, { method: "POST", body: { body } });
    input.value = "";
    await refreshMessages();
    await loadMatches();
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Utilitaires ------------------------------------------------------------

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
}

function useGeolocation() {
  if (!navigator.geolocation) {
    toast("Géolocalisation indisponible sur ce navigateur.", true);
    return;
  }
  navigator.geolocation.getCurrentPosition(
    ({ coords }) => {
      const form = $("#profile-form");
      form.elements.latitude.value = coords.latitude.toFixed(4);
      form.elements.longitude.value = coords.longitude.toFixed(4);
      toast("Position renseignée.");
    },
    () => toast("Position refusée : saisis les coordonnées à la main.", true),
  );
}

// --- Démarrage --------------------------------------------------------------

async function boot() {
  state.meta = await api("/api/meta");
  renderMeta();

  $("#auth-form").addEventListener("submit", (event) => {
    event.preventDefault();
    authenticate("/api/auth/login");
  });
  $("#register-btn").addEventListener("click", () => authenticate("/api/auth/register"));
  $("#profile-form").addEventListener("submit", saveProfile);
  $("#geo-btn").addEventListener("click", useGeolocation);
  $("#filter-form").addEventListener("submit", (event) => {
    event.preventDefault();
    loadDeck();
  });
  $("#chat-form").addEventListener("submit", sendMessage);
  $("#logout-btn").addEventListener("click", async () => {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } catch {
      /* la session locale est purgée quoi qu'il arrive */
    }
    signOut();
    toast("À bientôt sur la route.");
  });
  $$(".nav-btn[data-view]").forEach((button) =>
    button.addEventListener("click", () => showView(button.dataset.view)),
  );

  if (!state.token) {
    signOut();
    return;
  }
  try {
    const me = await api("/api/me");
    $("#nav").classList.remove("hidden");
    if (me.profile) {
      await fillProfileForm();
      showView("discover");
    } else {
      showView("profile");
    }
  } catch {
    signOut();
  }
}

boot();
