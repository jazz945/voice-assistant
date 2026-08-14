/* MotoMatch — client SPA sans dépendance. */

const state = {
  accessToken: sessionStorage.getItem("motomatch_access"),
  refreshToken: localStorage.getItem("motomatch_refresh"),
  userId: Number(localStorage.getItem("motomatch_user_id")) || null,
  meta: { bike_categories: [], riding_styles: [], pace_levels: [], report_reasons: [] },
  selectedStyles: new Set(),
  activeMatch: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

// --- Accès API --------------------------------------------------------------

async function rawRequest(path, method, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (state.accessToken) headers["Authorization"] = `Bearer ${state.accessToken}`;
  return fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// Un seul rafraîchissement à la fois : plusieurs 401 simultanés partagent la
// même promesse, sinon la rotation invaliderait les jetons les uns des autres.
let refreshInFlight = null;

async function refreshSession() {
  if (!state.refreshToken) return false;
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      const response = await fetch("/api/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: state.refreshToken }),
      });
      if (!response.ok) return false;
      storeSession(await response.json());
      return true;
    })().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

async function api(path, { method = "GET", body, retry = true } = {}) {
  let response = await rawRequest(path, method, body);

  // Le jeton d'accès est court : on le renouvelle en silence puis on rejoue.
  if (response.status === 401 && retry && !path.startsWith("/api/auth/")) {
    if (await refreshSession()) {
      response = await rawRequest(path, method, body);
    }
  }

  if (response.status === 401) {
    signOut();
    throw new Error("Session expirée, reconnecte-toi.");
  }
  if (response.status === 429) {
    const wait = response.headers.get("Retry-After");
    throw new Error(
      wait ? `Trop de tentatives. Réessaie dans ${wait} s.` : "Trop de tentatives, réessaie plus tard.",
    );
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
    return detail
      .map((item) =>
        typeof item === "string" ? item : `${(item.loc || []).slice(1).join(".")} : ${item.msg}`,
      )
      .join(" · ");
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
  if (name === "security") loadSecurity();
  if (name === "crossings") loadCrossings();
  if (name === "rides") loadRides();
  if (name === "plus") loadPlus();
}

// Le jeton d'accès vit en sessionStorage (effacé à la fermeture de l'onglet),
// le jeton de rafraîchissement en localStorage pour garder la session ouverte.
function storeSession(session) {
  state.accessToken = session.access_token;
  state.refreshToken = session.refresh_token;
  state.userId = session.user_id;
  sessionStorage.setItem("motomatch_access", session.access_token);
  localStorage.setItem("motomatch_refresh", session.refresh_token);
  localStorage.setItem("motomatch_user_id", String(session.user_id));
}

function signIn(session) {
  storeSession(session);
  $("#nav").classList.remove("hidden");
  showView(session.has_profile ? "discover" : "profile");
  if (!session.has_profile) toast("Complète ton profil moto pour commencer.");
}

function signOut() {
  state.accessToken = null;
  state.refreshToken = null;
  state.userId = null;
  sessionStorage.removeItem("motomatch_access");
  localStorage.removeItem("motomatch_refresh");
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
  const isRegistration = path.endsWith("/register");
  if (isRegistration) {
    if (!$("#age-attestation").checked) {
      toast("Confirme que tu as 18 ans ou plus pour t'inscrire.", true);
      return;
    }
    data.age_attestation = true;
  }
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

  $("#ride-pace").innerHTML = state.meta.pace_levels
    .map((p) => `<option value="${p}">${label(p)}</option>`)
    .join("");
  $("#ride-route").innerHTML = (state.meta.ride_route_types || [])
    .map((r) => `<option value="${r}">${label(r)}</option>`)
    .join("");
  $("#ride-visibility").innerHTML = (state.meta.ride_visibilities || [])
    .map((v) => `<option value="${v}">${label(v)}</option>`)
    .join("");
  $("#ride-categories").innerHTML = state.meta.bike_categories
    .map((c) => `<span class="chip" data-ride-category="${c}">${label(c)}</span>`)
    .join("");
  $$("#ride-categories .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const category = chip.dataset.rideCategory;
      if (selectedRideCategories.has(category)) selectedRideCategories.delete(category);
      else selectedRideCategories.add(category);
      chip.classList.toggle("selected");
    });
  });

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
  bindSafetyButtons(deck);
}

function bindSafetyButtons(container) {
  container.querySelectorAll("[data-block]").forEach((button) => {
    button.addEventListener("click", () => blockUser(Number(button.dataset.block)));
  });
  container.querySelectorAll("[data-report]").forEach((button) => {
    button.addEventListener("click", () =>
      openReportDialog(Number(button.dataset.report), button.dataset.name),
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
    <article class="rider ${categoryClass(p.bike_category)}">
      <div class="rider-head">
        ${avatarMarkup(p)}
        <div class="rider-identity">
          <h3>${escapeHtml(p.display_name)}, ${p.age}</h3>
          <p class="rider-sub">${escapeHtml(p.city)} · ${item.distance_km} km</p>
          <span class="chip famille">${escapeHtml(p.bike_category)}</span>
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
      <div class="rider-safety">
        <button class="link-btn" data-block="${p.user_id}">Bloquer</button>
        <button class="link-btn danger-text" data-report="${p.user_id}"
                data-name="${escapeHtml(p.display_name)}">Signaler</button>
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
        <li data-match="${match.match_id}" data-name="${escapeHtml(match.profile.display_name)}"
            class="${categoryClass(match.profile.bike_category)}">
          ${avatarMarkup(match.profile, 40)}
          <div>
            <div class="match-name">${escapeHtml(match.profile.display_name)}</div>
            <div class="match-preview">${escapeHtml(
              match.last_message || `${match.profile.bike_brand} ${match.profile.bike_model}`,
            )}</div>
          </div>
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

// --- Croisements ------------------------------------------------------------

async function loadCrossings() {
  try {
    const me = await api("/api/me");
    const enabled = Boolean(me.crossings_enabled);
    $("#crossings-toggle").checked = enabled;
    $("#crossings-actions").classList.toggle("hidden", !enabled);

    const list = $("#crossings-list");
    if (!enabled) {
      list.innerHTML =
        '<p class="empty">Active les croisements pour voir qui tu as croisé sur la route.</p>';
      return;
    }
    const data = await api("/api/crossings");
    list.innerHTML = data.results.length
      ? data.results.map(renderCrossingCard).join("")
      : '<p class="empty">Personne de croisé pour l\'instant. Roule un peu.</p>';
    list.querySelectorAll("[data-salut]").forEach((button) => {
      button.addEventListener("click", () => sendSalut(Number(button.dataset.salut)));
    });
    bindSafetyButtons(list);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderCrossingCard(item) {
  const p = item.profile;
  // Le sens inverse est le vrai croisement motard : on le met en avant.
  const badge =
    item.direction === "sens-inverse"
      ? '<span class="badge strong">sens inverse</span>'
      : `<span class="badge">${escapeHtml(item.direction.replaceAll("-", " "))}</span>`;
  const salut = item.salut_received
    ? item.salut_sent
      ? '<span class="badge strong">salut rendu · match</span>'
      : '<span class="badge strong">t\'a salué</span>'
    : "";

  return `
    <article class="rider ${categoryClass(p.bike_category)}">
      <div class="rider-head">
        ${avatarMarkup(p)}
        <div class="rider-identity">
          <h3>${escapeHtml(p.display_name)}, ${p.age}</h3>
          <p class="rider-sub">${escapeHtml(p.city)} · ${escapeHtml(item.last_seen_at)}</p>
          <span class="chip famille">${escapeHtml(p.bike_category)}</span>
        </div>
        <div class="crossing-mark">${item.times}×</div>
      </div>
      <p class="crossing-summary">${escapeHtml(item.summary)}</p>
      <div class="chips">${badge}
        <span class="badge">${escapeHtml(item.context)}</span>${salut}</div>
      <div class="bike">
        <strong>${escapeHtml(p.bike_brand)} ${escapeHtml(p.bike_model)}</strong>
        <div class="bike-meta">${p.engine_cc} cm³ · ${escapeHtml(p.bike_category)}</div>
      </div>
      <div class="rider-actions">
        <button class="btn-like" data-salut="${item.crossing_id}"
                ${item.salut_sent ? "disabled" : ""}>
          ${item.salut_sent ? "Salut envoyé" : "Faire un signe"}
        </button>
      </div>
      <div class="rider-safety">
        <button class="link-btn" data-block="${p.user_id}">Bloquer</button>
        <button class="link-btn danger-text" data-report="${p.user_id}"
                data-name="${escapeHtml(p.display_name)}">Signaler</button>
      </div>
    </article>`;
}

async function sendSalut(crossingId) {
  try {
    const result = await api(`/api/crossings/${crossingId}/salut`, { method: "POST" });
    toast(
      result.salut_returned
        ? "Salut rendu — c'est un match !"
        : "Signe envoyé. S'il te le rend, vous matchez.",
    );
    await loadCrossings();
  } catch (error) {
    toast(error.message, true);
  }
}

async function toggleCrossings(event) {
  try {
    const result = await api("/api/me/crossings", {
      method: "PUT",
      body: { enabled: event.target.checked },
    });
    toast(
      result.enabled
        ? "Croisements activés."
        : `Croisements coupés, ${result.purged_pings} position(s) effacée(s).`,
    );
    await loadCrossings();
  } catch (error) {
    toast(error.message, true);
    await loadCrossings();
  }
}

function sendPing() {
  if (!navigator.geolocation) {
    toast("Géolocalisation indisponible sur ce navigateur.", true);
    return;
  }
  navigator.geolocation.getCurrentPosition(
    async ({ coords }) => {
      try {
        const result = await api("/api/crossings/ping", {
          method: "POST",
          body: {
            latitude: coords.latitude,
            longitude: coords.longitude,
            speed_kmh: coords.speed === null ? null : coords.speed * 3.6,
            heading_deg: coords.heading === null ? null : coords.heading,
          },
        });
        toast(
          result.new_crossings
            ? `${result.new_crossings} nouveau(x) croisement(s) !`
            : "Position prise en compte.",
        );
        await loadCrossings();
      } catch (error) {
        toast(error.message, true);
      }
    },
    () => toast("Position refusée.", true),
    { enableHighAccuracy: true },
  );
}

async function purgeCrossings() {
  if (!confirm("Effacer définitivement tous tes croisements et positions ?")) return;
  try {
    const result = await api("/api/crossings", { method: "DELETE" });
    toast(`${result.deleted_rows} enregistrement(s) effacé(s).`);
    await loadCrossings();
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Balades ----------------------------------------------------------------

const selectedRideCategories = new Set();

async function loadRides() {
  try {
    const data = await api("/api/rides");
    const list = $("#rides-list");
    list.innerHTML = data.results.length
      ? data.results.map(renderRideCard).join("")
      : '<p class="empty">Aucune balade à venir. Lance la première.</p>';
    list.querySelectorAll("[data-join]").forEach((button) => {
      button.addEventListener("click", () => joinRide(Number(button.dataset.join)));
    });
    list.querySelectorAll("[data-leave]").forEach((button) => {
      button.addEventListener("click", () => leaveRide(Number(button.dataset.leave)));
    });
    list.querySelectorAll("[data-cancel]").forEach((button) => {
      button.addEventListener("click", () => cancelRide(Number(button.dataset.cancel)));
    });
  } catch (error) {
    toast(error.message, true);
  }
}

const VISIBILITY_LABEL = {
  public: "ouverte à tous",
  matchs: "réservée à mes matchs",
  "sur-demande": "sur validation",
};

function renderRideCard(ride) {
  const when = new Date(ride.start_at).toLocaleString("fr-FR", {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
  const categories = (ride.bike_categories || [])
    .map((c) => `<span class="chip static">${escapeHtml(c)}</span>`)
    .join("");

  let action = `<button class="btn-like" data-join="${ride.id}">Rejoindre</button>`;
  if (ride.is_organiser) {
    action = `<button class="btn-pass" data-cancel="${ride.id}">Annuler la balade</button>`;
  } else if (ride.my_status === "accepte") {
    action = `<button class="btn-pass" data-leave="${ride.id}">Je me désiste</button>`;
  } else if (ride.my_status === "demande") {
    action = `<button class="btn-pass" disabled>Demande en attente</button>`;
  } else if (ride.spots_left === 0) {
    action = `<button class="btn-pass" disabled>Complet</button>`;
  }

  return `
    <article class="rider">
      <div class="rider-head">
        <div>
          <h3>${escapeHtml(ride.title)}</h3>
          <p class="rider-sub">
            ${escapeHtml(when)} · départ ${escapeHtml(ride.start_city)} · ${ride.distance_km} km
          </p>
        </div>
        <div class="score">${ride.accepted_count}/${ride.max_participants}<small>motards</small></div>
      </div>
      ${ride.description ? `<p class="bio">${escapeHtml(ride.description)}</p>` : ""}
      <div class="bike">
        <strong>${escapeHtml(ride.route_type)}</strong>
        <div class="bike-meta">
          rythme ${escapeHtml(ride.pace.replaceAll("-", " "))} ·
          ${escapeHtml(VISIBILITY_LABEL[ride.visibility] || ride.visibility)} ·
          organisée par ${escapeHtml(ride.organiser_name || "—")}
          ${ride.distance_from_you_km !== undefined ? ` · départ à ${ride.distance_from_you_km} km de toi` : ""}
        </div>
      </div>
      ${categories ? `<div class="chips">${categories}</div>` : ""}
      ${
        ride.start_latitude === undefined
          ? '<p class="muted small">Point de rendez-vous exact visible une fois inscrit.</p>'
          : ""
      }
      <div class="rider-actions">${action}</div>
    </article>`;
}

async function createRide(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  ["start_latitude", "start_longitude", "distance_km", "max_participants"].forEach((key) => {
    data[key] = Number(data[key]);
  });
  // <input datetime-local> ne porte pas de fuseau : on le complète.
  data.start_at = new Date(data.start_at).toISOString();
  data.bike_categories = Array.from(selectedRideCategories);
  try {
    await api("/api/rides", { method: "POST", body: data });
    event.target.reset();
    selectedRideCategories.clear();
    $$("#ride-categories .chip").forEach((chip) => chip.classList.remove("selected"));
    toast("Balade créée.");
    await loadRides();
  } catch (error) {
    toast(error.message, true);
  }
}

async function joinRide(rideId) {
  try {
    const result = await api(`/api/rides/${rideId}/join`, { method: "POST" });
    toast(
      result.status === "demande"
        ? "Demande envoyée, l'organisateur doit valider."
        : "Tu es inscrit. Bonne route !",
    );
    await loadRides();
  } catch (error) {
    toast(error.message, true);
  }
}

async function leaveRide(rideId) {
  try {
    await api(`/api/rides/${rideId}/join`, { method: "DELETE" });
    toast("Désistement enregistré.");
    await loadRides();
  } catch (error) {
    toast(error.message, true);
  }
}

async function cancelRide(rideId) {
  if (!confirm("Annuler cette balade pour tous les participants ?")) return;
  try {
    await api(`/api/rides/${rideId}`, { method: "DELETE" });
    toast("Balade annulée.");
    await loadRides();
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Abonnement -------------------------------------------------------------

async function loadPlus() {
  try {
    const [abo, offres, likes] = await Promise.all([
      api("/api/subscription"),
      api("/api/subscription/offers"),
      api("/api/likes/received"),
    ]);
    renderStatut(abo);
    renderOffres(offres.offers, abo);
    renderLikesRecus(likes);
  } catch (error) {
    toast(error.message, true);
  }
}

function renderStatut(abo) {
  const q = abo.likes;
  const boosts = abo.boosts;
  const plus = abo.tier === "plus";

  $("#plus-status").innerHTML = `
    <h2>${plus ? "MotoMatch Plus" : "Compte gratuit"}</h2>
    <div class="chips">
      <span class="badge${plus ? " strong" : ""}">${plus ? "abonné" : "gratuit"}</span>
      ${abo.expires_at ? `<span class="badge">jusqu'au ${escapeHtml(abo.expires_at.slice(0, 10))}</span>` : ""}
      ${abo.cancelled_at ? '<span class="badge">résilié</span>' : ""}
    </div>
    <p class="muted small">
      ${q.unlimited
        ? "Likes illimités."
        : `${q.remaining} like(s) restant(s) aujourd'hui sur ${q.limit}. Remise à zéro à minuit.`}
    </p>
    <p class="muted small">
      Boosts : ${boosts.used_this_month}/${boosts.included} utilisés ce mois-ci.
      ${boosts.active_until ? `Boost en cours jusqu'à ${escapeHtml(boosts.active_until)}.` : ""}
    </p>
    <div class="row">
      ${plus && boosts.included > 0 ? '<button class="primary" id="boost-btn">Booster mon profil</button>' : ""}
      ${plus ? '<button class="secondary" id="rewind-btn">Annuler mon dernier swipe</button>' : ""}
      ${plus && !abo.cancelled_at ? '<button class="link-btn danger-text" id="cancel-abo">Résilier</button>' : ""}
    </div>`;

  const boost = $("#boost-btn");
  if (boost) boost.addEventListener("click", lancerBoost);
  const rewind = $("#rewind-btn");
  if (rewind) rewind.addEventListener("click", annulerDernierSwipe);
  const cancel = $("#cancel-abo");
  if (cancel) cancel.addEventListener("click", resilier);
}

function renderOffres(offres, abo) {
  $("#offres").innerHTML = offres
    .map(
      (o) => `
      <article class="rider">
        <div class="rider-head">
          <div class="rider-identity">
            <h3>${escapeHtml(o.label)}</h3>
            <p class="rider-sub">${o.price.toFixed(2)} ${escapeHtml(o.currency)} / ${escapeHtml(o.period)}</p>
          </div>
        </div>
        <ul class="notes">${o.highlights.map((h) => `<li>${escapeHtml(h)}</li>`).join("")}</ul>
        <div class="rider-actions">
          <button class="btn-like" data-offre="${escapeHtml(o.code)}"
                  ${abo.tier === "plus" ? "disabled" : ""}>
            ${abo.tier === "plus" ? "Déjà abonné" : "S'abonner"}
          </button>
        </div>
      </article>`,
    )
    .join("");
  $("#offres").querySelectorAll("[data-offre]").forEach((b) => {
    b.addEventListener("click", () => souscrire(b.dataset.offre));
  });
}

function renderLikesRecus(likes) {
  const zone = $("#likes-recus");
  if (likes.locked) {
    zone.innerHTML = `
      <p class="empty">
        <strong>${likes.count}</strong> personne(s) t'ont liké.<br />
        ${escapeHtml(likes.message)}
      </p>`;
    return;
  }
  zone.innerHTML = likes.count
    ? likes.results
        .map((item) => {
          const p = item.profile;
          return `
            <article class="rider ${categoryClass(p.bike_category)}">
              <div class="rider-head">
                ${avatarMarkup(p)}
                <div class="rider-identity">
                  <h3>${escapeHtml(p.display_name)}, ${p.age}</h3>
                  <p class="rider-sub">${escapeHtml(p.city)}</p>
                  <span class="chip famille">${escapeHtml(p.bike_category)}</span>
                </div>
              </div>
              <div class="bike">
                <strong>${escapeHtml(p.bike_brand)} ${escapeHtml(p.bike_model)}</strong>
                <div class="bike-meta">${p.engine_cc} cm³</div>
              </div>
            </article>`;
        })
        .join("")
    : '<p class="empty">Personne pour le moment.</p>';
}

async function souscrire(code) {
  try {
    const r = await api("/api/subscription/checkout", {
      method: "POST",
      body: { offer_code: code },
    });
    toast(r.detail || "Paiement à brancher sur cette installation.");
  } catch (error) {
    toast(error.message, true);
  }
}

async function lancerBoost() {
  try {
    const r = await api("/api/boost", { method: "POST", body: {} });
    toast(`Boost lancé jusqu'à ${r.active_until}. Encore ${r.remaining_this_month} ce mois-ci.`);
    await loadPlus();
  } catch (error) {
    toast(error.message, true);
  }
}

async function annulerDernierSwipe() {
  try {
    await api("/api/swipes/last", { method: "DELETE" });
    toast("Dernier swipe annulé, le profil revient dans le deck.");
  } catch (error) {
    toast(error.message, true);
  }
}

async function resilier() {
  if (!confirm("Résilier l'abonnement ? L'accès reste ouvert jusqu'à l'échéance déjà payée.")) return;
  try {
    const r = await api("/api/subscription", { method: "DELETE" });
    toast(`Résilié. Accès conservé jusqu'au ${r.access_until || "terme"}.`);
    await loadPlus();
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Sécurité des personnes -------------------------------------------------

async function blockUser(userId) {
  if (!confirm("Bloquer cette personne ? Vous disparaîtrez mutuellement de l'application.")) return;
  try {
    await api("/api/blocks", { method: "POST", body: { target_user_id: userId } });
    toast("Personne bloquée.");
    await loadDeck();
  } catch (error) {
    toast(error.message, true);
  }
}

function openReportDialog(userId, name) {
  const dialog = $("#report-dialog");
  $("#report-target").textContent = name || "cette personne";
  dialog.dataset.target = String(userId);
  $("#report-reason").innerHTML = state.meta.report_reasons
    .map((reason) => `<option value="${reason}">${reason.replaceAll("-", " ")}</option>`)
    .join("");
  dialog.showModal();
}

async function submitReport(event) {
  event.preventDefault();
  const dialog = $("#report-dialog");
  const data = Object.fromEntries(new FormData(event.target));
  try {
    await api("/api/reports", {
      method: "POST",
      body: {
        target_user_id: Number(dialog.dataset.target),
        reason: data.reason,
        details: data.details || "",
      },
    });
    dialog.close();
    event.target.reset();
    toast("Signalement envoyé. Cette personne est aussi bloquée.");
    await loadDeck();
  } catch (error) {
    toast(error.message, true);
  }
}

// --- Écran Sécurité ---------------------------------------------------------

async function loadSecurity() {
  await Promise.all([loadSessions(), loadBlocks()]);
}

async function loadSessions() {
  try {
    const data = await api("/api/auth/sessions");
    $("#session-list").innerHTML = data.results
      .map(
        (session) => `
        <li>
          <div>
            <strong>${session.current ? "Cet appareil" : "Autre appareil"}</strong>
            <div class="muted">${escapeHtml(session.device_label || "appareil inconnu")}</div>
            <div class="muted">Dernière activité : ${escapeHtml(
              session.last_used_at || session.created_at,
            )}</div>
          </div>
          ${
            session.current
              ? ""
              : `<button class="link-btn danger-text" data-revoke="${session.id}">Révoquer</button>`
          }
        </li>`,
      )
      .join("");
    $$("#session-list [data-revoke]").forEach((button) => {
      button.addEventListener("click", async () => {
        await api(`/api/auth/sessions/${button.dataset.revoke}`, { method: "DELETE" });
        toast("Session révoquée.");
        await loadSessions();
      });
    });
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadBlocks() {
  try {
    const data = await api("/api/blocks");
    $("#block-list").innerHTML = data.results.length
      ? data.results
          .map(
            (block) => `
            <li>
              <span>${escapeHtml(block.display_name || `Utilisateur ${block.user_id}`)}</span>
              <button class="link-btn" data-unblock="${block.user_id}">Débloquer</button>
            </li>`,
          )
          .join("")
      : '<li class="muted">Personne de bloqué.</li>';
    $$("#block-list [data-unblock]").forEach((button) => {
      button.addEventListener("click", async () => {
        await api(`/api/blocks/${button.dataset.unblock}`, { method: "DELETE" });
        toast("Personne débloquée.");
        await loadBlocks();
      });
    });
  } catch (error) {
    toast(error.message, true);
  }
}

async function changePassword(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  try {
    const result = await api("/api/me/password", { method: "POST", body: data });
    event.target.reset();
    toast(`Mot de passe changé. ${result.revoked_other_sessions} autre(s) session(s) fermée(s).`);
    await loadSessions();
  } catch (error) {
    toast(error.message, true);
  }
}

async function exportData() {
  try {
    const data = await api("/api/me/export");
    // Affichage dans un nouvel onglet : le téléchargement direct est bloqué
    // dans certains contextes d'intégration.
    const window_ = window.open("", "_blank");
    if (window_) {
      window_.document.title = "Export MotoMatch";
      const pre = window_.document.createElement("pre");
      pre.textContent = JSON.stringify(data, null, 2);
      window_.document.body.appendChild(pre);
    } else {
      toast("Autorise les fenêtres surgissantes pour voir l'export.", true);
    }
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteAccount(event) {
  event.preventDefault();
  if (!confirm("Cette action est définitive et efface toutes tes données. Continuer ?")) return;
  const data = Object.fromEntries(new FormData(event.target));
  try {
    await api("/api/me", { method: "DELETE", body: data });
    signOut();
    toast("Compte supprimé. Bonne route.");
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
  $("#password-form").addEventListener("submit", changePassword);
  $("#delete-form").addEventListener("submit", deleteAccount);
  $("#report-form").addEventListener("submit", submitReport);
  $("#report-cancel").addEventListener("click", () => $("#report-dialog").close());
  $("#export-btn").addEventListener("click", exportData);
  $("#crossings-toggle").addEventListener("change", toggleCrossings);
  $("#ping-btn").addEventListener("click", sendPing);
  $("#purge-crossings-btn").addEventListener("click", purgeCrossings);
  $("#ride-form").addEventListener("submit", createRide);
  $("#logout-all-btn").addEventListener("click", async () => {
    if (!confirm("Déconnecter tous les appareils, y compris celui-ci ?")) return;
    try {
      await api("/api/auth/logout-all", { method: "POST" });
    } finally {
      signOut();
      toast("Toutes les sessions ont été fermées.");
    }
  });
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

  if (!state.accessToken && !state.refreshToken) {
    signOut();
    return;
  }
  // Onglet rouvert : le jeton d'accès a disparu, on le regagne par rotation.
  if (!state.accessToken && state.refreshToken) {
    await refreshSession();
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

// Service worker : rend l'application installable sur Android et iOS. Son
// absence n'empêche rien — l'application fonctionne sans.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* navigateur sans service worker, ou origine non sécurisée : sans effet */
    });
  });
}

boot();
