/* Avatars et couleurs par famille de moto.
 *
 * Deux règles derrière ce fichier :
 *
 * 1. La couleur porte une information. Chaque famille de moto a sa teinte, et
 *    cette teinte se retrouve partout où le profil apparaît — casque, filet de
 *    la carte, pastille de catégorie. On repère une sportive d'un coup d'œil
 *    dans le deck, sans lire.
 *
 * 2. À défaut de photo, on dessine un casque, pas un visage. Inventer un
 *    portrait pour quelqu'un qui n'a pas mis de photo serait un mensonge sur
 *    une application de rencontre ; un casque dit la même chose d'utile — quelle
 *    machine, quel univers — sans rien prétendre.
 */

const CATEGORY_COLOURS = {
  sportive: { light: "#ff7a92", dark: "#e11d48" },
  roadster: { light: "#ffa76b", dark: "#d96b22" },
  trail: { light: "#7ddfa0", dark: "#2f9e5c" },
  routiere: { light: "#7dbcff", dark: "#2f7fd4" },
  custom: { light: "#c9a8ff", dark: "#7c4dcc" },
  supermotard: { light: "#ffe07a", dark: "#d9a41a" },
  cross: { light: "#ffa26b", dark: "#e05a15" },
  scooter: { light: "#8fe8dc", dark: "#2f9e94" },
};

const FALLBACK_COLOUR = { light: "#b8c2cf", dark: "#69737f" };

function categoryColour(category) {
  return CATEGORY_COLOURS[String(category || "").toLowerCase()] || FALLBACK_COLOUR;
}

/** Casque dessiné, décliné en trois décors pour que deux profils voisins diffèrent. */
function helmetSvg(category, seed, size = 56) {
  const { light, dark } = categoryColour(category);
  // Identifiant unique par avatar : deux <defs> partageant un id se marcheraient
  // dessus, et tous les casques de la page prendraient le même dégradé.
  const uid = `h${category || "x"}${seed}`.replace(/[^a-z0-9]/gi, "");
  const variant = Math.abs(Number(seed) || 0) % 3;

  const decor = [
    `<path d="M32 8 L38 8 L34 58 L28 58 Z" fill="#0e1116" opacity=".18"/>`,
    `<path d="M10 34 C22 30 42 30 54 34 L54 39 C42 35 22 35 10 39 Z" fill="#0e1116" opacity=".16"/>`,
    ``,
  ][variant];

  return `
    <svg class="avatar" viewBox="0 0 64 64" role="img" aria-hidden="true"
         width="${size}" height="${size}">
      <defs>
        <linearGradient id="${uid}" x1="0" y1="0" x2="0.3" y2="1">
          <stop offset="0" stop-color="${light}"/>
          <stop offset="1" stop-color="${dark}"/>
        </linearGradient>
      </defs>
      <path d="M32 5 C47 5 55 16 55 30 L55 41 C55 53 46 59 32 59
               C18 59 9 53 9 41 L9 30 C9 16 17 5 32 5 Z" fill="url(#${uid})"/>
      ${decor}
      <path d="M15 27 C19 20 25 17 32 17 C39 17 45 20 49 27 L49 35
               C40 31 24 31 15 35 Z" fill="#0e1116" opacity=".9"/>
      <path d="M17 26 C21 21 26 19 31 19 L30 23 C26 24 22 26 19 29 Z"
            fill="#ffffff" opacity=".18"/>
      <rect x="21" y="47" width="22" height="7" rx="3.5" fill="#0e1116" opacity=".45"/>
    </svg>`;
}

function escapeAttribute(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
}

/**
 * Vignette d'un profil : sa photo si elle existe, son casque sinon.
 *
 * Le casque est toujours rendu, la photo se pose par-dessus. Une URL morte
 * laisse donc apparaître le casque, sans gestionnaire `onerror` — que le CSP
 * bloquerait de toute façon, puisqu'un attribut d'événement est du script en
 * ligne.
 *
 * `referrerpolicy` évite d'annoncer au serveur d'images quel profil est
 * consulté, et `loading="lazy"` épargne la bande passante sur un deck long.
 */
function avatarMarkup(profile, size = 56) {
  const helmet = helmetSvg(profile.bike_category, profile.user_id, size);
  const photo = (profile.photo_url || "").trim();
  if (!photo) return `<span class="avatar-slot">${helmet}</span>`;

  return `<span class="avatar-slot">
    ${helmet}
    <img class="avatar avatar-photo" src="${escapeAttribute(photo)}" alt=""
         width="${size}" height="${size}" loading="lazy" referrerpolicy="no-referrer" />
  </span>`;
}

/**
 * Classe de teinte à poser sur l'élément qui porte le profil.
 *
 * Une classe et non un attribut `style` : la politique de sécurité du contenu
 * interdit les styles en ligne (`style-src 'self'`), et l'affaiblir pour une
 * question de couleur serait un mauvais échange. Les teintes vivent donc dans
 * styles.css, en regard de `CATEGORY_COLOURS` ci-dessus.
 */
function categoryClass(category) {
  const known = Object.prototype.hasOwnProperty.call(
    CATEGORY_COLOURS, String(category || "").toLowerCase(),
  );
  return known ? `famille-${String(category).toLowerCase()}` : "famille-inconnue";
}
