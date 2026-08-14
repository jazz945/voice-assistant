# Sécurité de MotoMatch

Ce document décrit le modèle de menace retenu, les protections en place et — tout
aussi important — **ce qui reste à faire avant une mise en production réelle**.

Toutes les protections listées ici sont couvertes par des tests automatisés
(`motomatch/tests/test_security.py`), chacun écrit à partir d'une attaque
concrète plutôt que d'une case à cocher.

## Modèle de menace

Une application de rencontre géolocalisée attire des adversaires précis :

| Adversaire | Objectif | Protection principale |
|---|---|---|
| Harceleur | Localiser physiquement une personne | Grille géographique, coordonnées jamais exposées, blocage |
| Attaquant par force brute | Prendre le contrôle d'un compte | Argon2id, verrouillage progressif, limitation par IP et par compte |
| Voleur de jeton | Réutiliser une session interceptée | Jetons courts, rotation avec détection de rejeu, révocation immédiate |
| Collecteur de données | Aspirer les profils en masse | Limitation de débit, pagination bornée, aucune donnée de contact exposée |
| Curieux interne / fuite de base | Lire mots de passe et sessions | Argon2id, jetons hachés au repos, IP hachées dans le journal |
| Mineur cherchant à s'inscrire | Contourner la limite d'âge | Attestation obligatoire, âge validé, année de naissance figée |

## Authentification

**Mots de passe** — Argon2id (`argon2-cffi`), paramètres conformes aux
recommandations OWASP : 19 Mio de mémoire, 2 passes, parallélisme 1, sel de
16 octets. La politique suit l'esprit du NIST SP 800-63B : longueur minimale de
12 caractères, refus des mots de passe courants, des suites de touches et de
tout mot de passe contenant les données personnelles du compte — plutôt que des
règles de composition rigides qui poussent aux mots de passe prévisibles.

Les empreintes PBKDF2 de la version 1.0 restent vérifiables et sont **remigrées
vers Argon2id à la première connexion réussie**, sans action de l'utilisateur.

**Énumération de comptes** — quand l'adresse e-mail est inconnue, l'API exécute
quand même une vérification Argon2id sur une empreinte fictive
(`waste_time_like_a_verification`). Le temps de réponse ne distingue donc pas
« compte inexistant » de « mauvais mot de passe ». Un test mesure effectivement
les deux chemins.

**Force brute** — deux verrous indépendants, l'un par compte et l'autre par
adresse IP. Au-delà de 5 échecs dans une fenêtre de 15 minutes, la temporisation
double à chaque tentative supplémentaire (60 s, 120 s, 240 s…) et plafonne à une
heure. Le verrouillage n'est jamais définitif : sinon un attaquant pourrait
bloquer volontairement le compte de sa cible.

## Sessions

Le choix de jetons **opaques** plutôt que de JWT est délibéré : un JWT
auto-porteur ne peut pas être révoqué avant son expiration, alors qu'ici chaque
jeton est une ligne en base. « Déconnecter cet appareil » prend effet
immédiatement — ce qu'un téléphone volé exige.

- **Jeton d'accès** : 15 minutes par défaut.
- **Jeton de rafraîchissement** : 30 jours, à usage unique.
- **Aucun jeton n'est stocké en clair** : la base ne contient que leur SHA-256.
  Une fuite de la base ne permet pas d'usurper une session.
- **Rotation avec détection de rejeu** : chaque rafraîchissement invalide le
  précédent. Si un jeton déjà consommé est représenté, c'est qu'il a été copié :
  toute la famille de sessions est révoquée, y compris la session légitime. Mieux
  vaut une reconnexion qu'un intrus qui persiste.
- **Écran « appareils connectés »** avec révocation unitaire ou globale.
- Changer de mot de passe révoque toutes les autres sessions.

## Confidentialité de la localisation

C'est le risque le plus concret de ce type d'application. Si l'API renvoie une
distance précise, un attaquant qui déplace sa position déclarée trois fois
retrouve par trilatération l'adresse de sa cible. Tinder et Grindr ont tous deux
été vulnérables à cette attaque.

Trois mesures se combinent (`privacy.py`) :

1. **Les coordonnées d'autrui ne sortent jamais de l'API.** `latitude` et
   `longitude` sont retirées de tout profil vu par un tiers — découverte comme
   matchs. Seul l'utilisateur connecté lit les siennes.
2. **Les positions comparées sont plaquées sur une grille** de 1 km. La distance
   est calculée depuis le centre de la cellule, jamais depuis le point réel : la
   trilatération ne peut pas faire mieux que la cellule.
3. **Le décalage de grille est propre à chaque utilisateur et déterministe**,
   dérivé d'un HMAC du secret serveur. Le déterminisme est essentiel : un bruit
   tiré à chaque requête serait moyenné par un attaquant qui interroge en boucle,
   et la position vraie réapparaîtrait. Un test le vérifie explicitement.

La distance affichée est en outre arrondie par paliers de 5 km — utile, mais
insuffisant à soi seul, d'où la grille.

## Croisements (fonction « qui ai-je croisé »)

C'est la fonction la plus intrusive de l'application — un suivi de position, en
tension directe avec le reste de ce document. Elle est donc traitée à part :

- **Opt-in strict**, désactivée par défaut. Rien n'est enregistré tant que
  l'utilisateur ne l'a pas activée.
- **Aucune coordonnée GPS n'est jamais écrite en base.** La table
  `location_pings` ne comporte même pas de colonne `latitude` : la position est
  réduite dès la réception à un identifiant de cellule de 500 m et à un créneau
  de 15 minutes, puis jetée. Un test vérifie l'absence de ces colonnes.
- **Rétention de 24 heures** sur les positions ; seuls les croisements avérés
  survivent, et sans coordonnées.
- **Réciprocité obligatoire** : un croisement n'existe que si les deux personnes
  ont activé la fonction. On ne peut pas rester invisible tout en continuant à
  voir les autres.
- **Effacement immédiat** : couper la fonction purge les positions sur-le-champ,
  et `DELETE /api/crossings` efface tout l'historique des deux côtés.
- Les personnes bloquées ne se croisent jamais.
- La zone remontée est **le centre de la cellule**, jamais la position réelle de
  l'autre — et le demandeur y était lui-même. La cellule brute (`cell_id`) n'est
  jamais exposée : un test le vérifie explicitement, après qu'une jointure l'a
  effectivement laissée fuiter pendant le développement.

Ce que la fonction révèle, par construction : que deux personnes ayant toutes
deux consenti se sont trouvées dans la même zone de 500 m au même quart d'heure.
C'est le produit. Ce qu'elle ne révèle jamais : un trajet, une position exacte,
ou quoi que ce soit sur quelqu'un qui ne l'a pas activée.

## Paiement

L'activation d'un abonnement ne vient **jamais** du client. Une route « je viens
de payer » se déclenche depuis n'importe quel terminal : l'abonnement serait
gratuit pour qui sait envoyer une requête. `POST /api/subscription/checkout`
n'accorde donc aucun droit — il ouvre un paiement, rien de plus. Seul le webhook
du prestataire active, et il est vérifié sur trois points :

1. **Signature HMAC-SHA256**, comparée en temps constant. Une comparaison naïve
   laisserait fuir la signature attendue octet par octet.
2. **Horodatage** dans une fenêtre de 5 minutes. Sans cela, un message signé
   capté une fois se rejouerait indéfiniment pour prolonger un abonnement.
3. **Idempotence** par `UNIQUE (provider, event_id)`. Les prestataires rejouent
   leurs webhooks au moindre doute ; sans ce verrou, un rejeu prolongerait
   l'abonnement une seconde fois.

Le montant n'est jamais accepté du client : il choisit un code d'offre, le prix
vient du serveur.

Le journal `payment_events` n'a **pas** de clé étrangère vers `users` : un
évènement citant un compte inexistant est justement celui qu'on veut conserver
— quelqu'un sonde l'URL — et une contrainte ferait échouer l'écriture au moment
où elle sert le plus.

En cas d'échéance illisible, le compte retombe en gratuit. Se tromper dans ce
sens coûte un mécontentement ; dans l'autre, un abonnement gratuit à vie.

## Sécurité des personnes

Fonctions exigées par l'App Store (règle 1.2, contenu généré par les
utilisateurs) et par le Play Store :

- **Blocage** bidirectionnel : les deux personnes disparaissent mutuellement de
  la découverte, des matchs et de la messagerie, y compris pour une conversation
  déjà ouverte.
- **Discrétion du blocage** : une personne bloquée reçoit `404 profil
  introuvable`, jamais `403`. Elle ne doit pas apprendre qu'elle a été bloquée.
- **Signalement** avec motif typé, qui bloque automatiquement dans la foulée.
- **Point de rendez-vous protégé** : les coordonnées exactes du départ d'une
  balade ne sont données qu'aux participants acceptés et à l'organisateur. Une
  balade ouverte ne publie pas l'adresse précise d'un rendez-vous à qui n'y va
  pas, et une balade « sur validation » laisse l'organisateur filtrer avant de
  la livrer.
- **Attestation de majorité** à l'inscription, âge revalidé à la saisie du profil,
  et **année de naissance figée** ensuite : un compte créé mineur ne peut pas se
  vieillir après coup.

## Cycle de vie du compte

- **Suppression définitive** (`DELETE /api/me`), protégée par le mot de passe et
  une confirmation explicite. Les `ON DELETE CASCADE` emportent profil, sessions,
  swipes, matchs, messages, blocages et signalements ; un test vérifie table par
  table qu'il ne reste rien. Obligatoire pour l'App Store (règle 5.1.1(v)).
- **Export des données** (`GET /api/me/export`), au titre du droit à la
  portabilité (RGPD article 20). L'empreinte du mot de passe n'y figure jamais.

## Durcissement HTTP

- En-têtes sur toutes les réponses : CSP stricte (`frame-ancestors 'none'`,
  `object-src 'none'`, pas de script en ligne), `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`,
  `Cross-Origin-Opener-Policy`. HSTS en production uniquement.
- `Cache-Control: no-store` sur toute réponse `/api/` : elles contiennent des
  données personnelles.
- CORS restreint à une liste d'origines, sans identifiants (l'authentification
  passe par un en-tête, pas un cookie), donc pas de CSRF possible.
- `TrustedHostMiddleware` contre l'empoisonnement d'en-tête `Host`.
- Corps de requête plafonné (256 Kio par défaut).
- **Aucune trace d'exécution ne sort de l'API** : toute exception non gérée
  devient `{"detail": "erreur interne"}`. Les erreurs de validation sont réduites
  à `loc`/`msg`/`type`, sans le `ctx` de Pydantic qui contient la valeur reçue —
  laquelle peut être un mot de passe.
- La documentation `/docs` est désactivable, et **doit** l'être en production.

## Configuration

`config.py` **refuse de démarrer en production** si un réglage sensible est resté
sur sa valeur de développement : secret par défaut ou trop court, `TRUSTED_HOSTS`
à `*`, origine CORS en `http://`, documentation activée. C'est un garde-fou
volontairement brutal : une mauvaise configuration doit faire échouer le
déploiement, pas passer inaperçue.

```bash
MOTOMATCH_ENV=production
MOTOMATCH_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
MOTOMATCH_TRUSTED_HOSTS=api.motomatch.example
MOTOMATCH_CORS_ORIGINS=https://motomatch.example
MOTOMATCH_DOCS_ENABLED=false
```

## Journal d'audit

Les évènements de sécurité (connexion, échec, verrouillage, rejeu de jeton,
changement de mot de passe, blocage, signalement, suppression) sont journalisés.
Les adresses IP y sont **hachées en HMAC** avec le secret serveur : comparables
entre elles, non réversibles. Le journal sert à répondre à « ce compte a-t-il été
compromis ? », pas à tracer les utilisateurs.

## Injection SQL

Toutes les requêtes sont paramétrées, sans exception ni concaténation de
chaînes. Les seuls fragments SQL construits dynamiquement sont des listes de
noms de colonnes issues de constantes du code, jamais d'une entrée utilisateur.

## Limites connues

Rien de ce qui suit n'est masqué : ce sont les points à traiter avant une
ouverture au public.

1. **Pas de vérification d'adresse e-mail.** Conséquence directe : l'inscription
   renvoie `409` si l'adresse existe déjà, ce qui révèle l'existence d'un compte.
   La correction propre est l'envoi d'un lien de confirmation avec une réponse
   générique dans tous les cas. En attendant, l'inscription est fortement limitée
   en débit par IP (5 par heure).
2. **Pas de réinitialisation de mot de passe** (elle dépend de l'envoi d'e-mails).
3. **Pas de second facteur.** TOTP est le prochain ajout logique.
4. **Photos non hébergées ni modérées** : ce sont des URL HTTPS externes. Un
   envoi de fichiers réel impose stockage privé, analyse antivirus et modération
   automatisée des contenus à caractère sexuel — obligatoire pour les stores.
5. **Signalements sans interface de modération** : ils sont stockés, mais aucune
   file de traitement humaine n'existe. Les stores exigent une action sous
   24 heures sur les contenus signalés.
6. **Limitation de débit mono-processus.** Les compteurs vivent dans SQLite : au
   -delà d'un processus, il faut les déplacer vers Redis. Seule la fonction
   `_count_recent` de `ratelimit.py` est à réécrire.
7. **Pas de chiffrement des messages de bout en bout.** Les messages sont lisibles
   côté serveur — nécessaire pour la modération, mais à annoncer clairement.
8. **SQLite** convient au développement et à un petit volume ; PostgreSQL avec
   chiffrement au repos s'impose en production.
9. **Le champ `seeking` est du texte libre** et n'est pas utilisé comme filtre.
10. **Croisements : pas de détection d'usurpation de position.** Un client
    modifié peut déclarer une position fausse pour provoquer des croisements
    fictifs. Les parades usuelles (attestation d'intégrité de l'application,
    contrôle de plausibilité des vitesses entre deux pings) restent à ajouter.
11. **Balades sans messagerie de groupe** ni rappel avant le départ.
12. **Aucun prestataire de paiement n'est branché.** La vérification des
    webhooks est complète et testée, mais la création des sessions de paiement
    demande des clés d'API et un compte. Sur iOS et Android, la facturation
    d'Apple et de Google est de toute façon obligatoire (15 à 30 %), et
    contourner par un prestataire tiers fait rejeter l'application.
13. **Obligations légales non couvertes** : société déclarée, TVA, CGV, droit de
    rétractation de 14 jours sur un abonnement en France. Ce n'est pas du code.
14. **Pas d'audit externe.** Aucune revue de sécurité indépendante n'a été menée
    sur ce code.

## Signaler une vulnérabilité

Pour un déploiement réel, publier une adresse de contact sécurité et un fichier
`.well-known/security.txt`, et s'engager sur un délai de première réponse.
