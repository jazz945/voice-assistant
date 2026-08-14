# Application Android MotoMatch

Coque native autour du client web de MotoMatch : icône de lanceur, plein écran,
et surtout le **pont de permission de géolocalisation**, sans lequel la fonction
« croisements » ne reçoit aucune position.

## Récupérer l'APK sans rien installer

Le SDK Android n'est pas nécessaire sur ta machine : GitHub Actions compile
l'APK à chaque poussée sur `android/`.

1. Onglet **Actions** du dépôt → workflow **APK Android**
2. Ouvrir la dernière exécution réussie
3. Section **Artifacts** → télécharger `motomatch-apk`
4. Décompresser, transférer `motomatch-debug.apk` sur le téléphone
5. L'ouvrir : Android demandera d'autoriser l'installation depuis cette source

## Compiler localement

```bash
cd android
./gradlew assembleDebug
# APK produit : app/build/outputs/apk/debug/app-debug.apk
```

Prérequis : JDK 17 et le SDK Android (API 34). Android Studio les installe tous
les deux.

Installation directe sur un téléphone branché en USB, débogage activé :

```bash
./gradlew installDebug
```

## Premier lancement

L'application ne contient pas d'adresse de serveur en dur — MotoMatch
s'auto-héberge, il n'y a pas de service central. Au premier lancement, elle
demande l'adresse de ton serveur, puis la mémorise.

- `https://` est ajouté par défaut si le préfixe est omis.
- **HTTP en clair : autorisé en compilation de débogage, interdit en release.**
  Le format de `network_security_config.xml` ne sait pas exprimer une plage
  d'adresses, il n'y a donc pas moyen d'autoriser « seulement le réseau local ».
  Le compromis retenu : `src/debug/` autorise le clair pour permettre les essais
  contre un serveur local, `src/main/` l'interdit pour la production.
  **L'APK produit par la CI est une compilation de débogage** : il accepte donc
  le HTTP en clair. À ne pas utiliser sur un réseau public — sers-toi d'un
  tunnel HTTPS dès que tu sors de chez toi.

Pour changer de serveur plus tard : vider les données de l'application dans les
réglages Android.

## Ce que fait la coque

| | |
|---|---|
| Géolocalisation | Enchaîne la permission Android puis la réponse à la page. C'est le seul vrai gain sur le navigateur. |
| Navigation | Le bouton retour remonte l'historique web ; les liens externes partent vers le navigateur, pour qu'aucune page tierce ne s'affiche sous l'identité de l'application. |
| Sauvegarde | Désactivée : les préférences contiennent l'adresse du serveur et le WebView les jetons de session. |
| Thème | Fond sombre dès le lancement, pour éviter le flash blanc avant le chargement de la page. |

Aucune interface JavaScript n'est exposée vers Kotlin (`addJavascriptInterface`
n'est pas utilisé) : la page web ne peut appeler aucun code natif.

## Limites

- **APK signé avec la clé de débogage.** Installable à la main, **pas**
  publiable sur le Play Store. Une publication demande une clé de release, un
  App Bundle (`.aab`) et un compte développeur — voir
  [`../motomatch/DEPLOIEMENT_STORES.md`](../motomatch/DEPLOIEMENT_STORES.md).
- **Pas de géolocalisation en arrière-plan.** Comme la PWA, les croisements ne
  se détectent que pendant que l'application est ouverte. Y remédier suppose un
  service de premier plan avec notification permanente, et une justification
  écrite à Google pour la permission `ACCESS_BACKGROUND_LOCATION` — c'est une
  des permissions les plus contrôlées du Play Store.
- **Pas de notifications push.**
- L'interface reste servie par le serveur : hors ligne, l'application ne montre
  rien.

Une alternative à cette coque WebView est le **TWA** (*Trusted Web Activity*),
qui publie la PWA telle quelle sur le Play Store. Elle exige un nom de domaine
fixe en HTTPS et un fichier Digital Asset Links — d'où le choix du WebView ici,
qui fonctionne quelle que soit l'adresse du serveur.
