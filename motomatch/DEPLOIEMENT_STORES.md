# Déploiement sur l'App Store et Google Play

Ce document décrit ce qui est **déjà en place côté backend** et ce qui reste à
construire pour publier MotoMatch sur les deux magasins d'applications.

À lire d'abord, franchement : le backend est prêt à recevoir un client mobile,
mais **une application de rencontre est parmi les catégories les plus contrôlées
des deux stores**. Le code n'est pas le principal obstacle — la modération, la
vérification d'âge et les mentions légales le sont.

## Ce que le backend fournit déjà

L'API est conçue pour être consommée par un client natif :

- authentification par jetons porteurs, sans cookie ni session serveur ;
- jeton d'accès court (15 min) + jeton de rafraîchissement rotatif (30 jours),
  adapté à une application qui reste installée des mois ;
- endpoint `/api/auth/sessions` pour l'écran « appareils connectés » ;
- CORS non requis pour un client natif (il n'envoie pas d'en-tête `Origin`) ;
- toutes les fonctions de sécurité exigées par les stores : blocage,
  signalement, suppression de compte, export des données, attestation d'âge ;
- `GET /api/meta` expose les valeurs autorisées, pour éviter de les dupliquer
  en dur dans le client.

### Stockage des jetons côté mobile

Point le plus important pour un client natif : **le jeton de rafraîchissement ne
doit jamais aller dans `AsyncStorage`, `UserDefaults` ou `SharedPreferences` en
clair.**

| Plateforme | Emplacement correct |
|---|---|
| iOS | Trousseau (Keychain), attribut `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` |
| Android | Keystore via `EncryptedSharedPreferences` |
| React Native / Expo | `expo-secure-store` (s'appuie sur les deux ci-dessus) |
| Flutter | `flutter_secure_storage` |

Le jeton d'accès peut rester en mémoire vive : il se regagne par rotation.

## Ce qu'il reste à construire

### 1. Le client mobile

Non commencé — c'est la prochaine étape convenue. Un seul code React Native
(Expo) ou Flutter couvre les deux plateformes.

### 2. Bloquants pour la validation

Ces points font rejeter une application de rencontre à coup sûr :

- **Modération sous 24 h** des contenus signalés, avec équipe humaine. Les
  signalements sont déjà collectés, mais aucune file de traitement n'existe.
- **Envoi et modération des photos.** Aujourd'hui les photos sont des URL
  externes. Il faut un stockage privé, une analyse antivirus et une détection
  automatisée de nudité, plus une revue humaine.
- **Vérification d'âge renforcée.** L'attestation déclarative en place est le
  minimum ; les stores demandent davantage pour une application de rencontre —
  vérification par pièce d'identité ou estimation d'âge sur selfie.
- **Vérification de l'adresse e-mail** (voir `SECURITY.md`, limite n° 1).
- **Mentions légales publiées** : politique de confidentialité et conditions
  d'utilisation accessibles par URL publique, exigées par les deux stores.

### 3. Obligations propres à Apple

- **Classement 18+** obligatoire, avec description explicite du contrôle d'âge.
- **Suppression de compte depuis l'application** (règle 5.1.1(v)) : l'API le fait
  déjà, le client doit exposer le bouton sans renvoyer vers un site web.
- **Sign in with Apple** devient obligatoire dès qu'un autre service
  d'identification tiers est proposé (Google, Facebook…). Tant que seul le couple
  e-mail/mot de passe existe, ce n'est pas requis.
- **Nutrition Privacy Label** à remplir dans App Store Connect : déclarer la
  localisation approximative, l'adresse e-mail et le contenu des messages.
- **Justification d'usage de la localisation** dans `Info.plist`
  (`NSLocationWhenInUseUsageDescription`), rédigée en clair.

### 4. Obligations propres à Google Play

- **Data safety form** : mêmes déclarations que le label Apple.
- **Déclaration des permissions sensibles**, notamment la localisation.
- **Politique « Rencontres »** : justifier les mécanismes anti-fraude et
  anti-faux profils.
- **Suppression de compte** accessible aussi depuis une **URL web publique**, en
  plus de l'application — exigence spécifique à Google.

### 5. Infrastructure

- HTTPS obligatoire de bout en bout, avec HSTS (déjà émis en mode production).
- **App Transport Security** (iOS) et **Network Security Config** (Android)
  laissés en refus du trafic en clair.
- Migration SQLite → PostgreSQL, avec chiffrement au repos et sauvegardes
  chiffrées testées.
- Compteurs de limitation de débit vers Redis (voir `SECURITY.md`, limite n° 6).
- Notifications push (APNs / FCM) pour les matchs et messages.
- Supervision : alerte sur les pics de `login.failure` et de
  `session.refresh_reuse_detected` dans le journal d'audit.

### 6. Conformité

- RGPD : l'export et la suppression sont en place ; restent le registre des
  traitements, la base légale et un contrat de sous-traitance avec l'hébergeur.
- Conservation des données : définir une durée et purger les comptes inactifs.
- Désignation d'un DPO si le volume le justifie.

## Ordre recommandé

1. Client mobile sur l'API actuelle, testé sur appareil réel.
2. Vérification d'e-mail et réinitialisation de mot de passe.
3. Envoi et modération des photos.
4. Interface de modération et procédure de traitement sous 24 h.
5. Mentions légales, labels de confidentialité, formulaires des stores.
6. Migration PostgreSQL + Redis, supervision, sauvegardes.
7. Audit de sécurité externe avant ouverture au public.
