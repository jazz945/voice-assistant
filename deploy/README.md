# Héberger MotoMatch chez soi

Kit de déploiement : l'application derrière un reverse proxy Caddy qui gère le
HTTPS, avec sauvegardes automatiques et redémarrage au boot.

C'est aussi ce qui débloque le reste : **l'APK Android et l'installation PWA ont
tous deux besoin d'un serveur qui tourne**, et l'installation PWA exige du HTTPS.

## Démarrage

```bash
cd deploy
cp .env.example .env
```

Puis éditer `.env`. Deux valeurs sont indispensables :

```bash
# Générer la clé de signature
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

- `MOTOMATCH_SECRET_KEY` — la clé générée ci-dessus. **La changer invalide
  toutes les sessions** et redistribue les décalages de grille géographique. À
  traiter comme un mot de passe : elle ne doit jamais être versionnée.
- `MOTOMATCH_SITE_ADDRESS` — l'adresse à laquelle le site répondra.

Ensuite :

```bash
docker compose up -d
docker compose logs -f app
```

Charger les profils de démonstration, une seule fois si tu veux essayer :

```bash
docker compose exec app python -m motomatch.seed
```

## Choisir son mode HTTPS

C'est la seule décision réelle du déploiement.

### Nom de domaine réel — le plus simple à l'usage

Un sous-domaine pointant vers ton IP publique, ports 80 et 443 redirigés vers la
machine. Caddy obtient et renouvelle seul un certificat Let's Encrypt.

```bash
CADDYFILE=Caddyfile
MOTOMATCH_SITE_ADDRESS=motomatch.mondomaine.fr
MOTOMATCH_TRUSTED_HOSTS=motomatch.mondomaine.fr
```

Certificat reconnu par tous les navigateurs, PWA installable, APK content. C'est
le mode recommandé.

### Réseau local seul — sans rien exposer

```bash
CADDYFILE=Caddyfile.lan
MOTOMATCH_SITE_ADDRESS=motomatch.maison
MOTOMATCH_TRUSTED_HOSTS=motomatch.maison
```

Caddy signe alors avec sa propre autorité de certification. **Les navigateurs ne
la connaissent pas** : ils afficheront un avertissement tant que sa racine n'est
pas installée sur chaque appareil.

```bash
# Récupérer la racine, puis l'installer sur les téléphones
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .
```

Sur Android : *Paramètres → Sécurité → Chiffrement → Installer un certificat →
Certificat CA*. Il faut aussi que `motomatch.maison` résolve sur ton réseau
(entrée statique dans le DNS de la box, ou Pi-hole / AdGuard).

Sans ce certificat installé, **la PWA n'est pas installable** : Chrome exige une
origine de confiance.

### Une alternative qui évite les deux

Un tunnel (Cloudflare Tunnel, Tailscale Funnel) donne une adresse HTTPS valide
sans ouvrir de port ni acheter de domaine. Rien à changer dans ce kit : viser
`http://localhost:8000` côté tunnel et laisser Caddy de côté.

## Ce que fait la composition

| Service | Rôle |
|---|---|
| `app` | L'application. Aucun port publié : joignable seulement à travers Caddy. |
| `caddy` | HTTPS, compression, sonde de santé sur `/api/health`. |
| `backup` | Instantané SQLite quotidien dans `deploy/sauvegardes/`, 14 conservés. |

`restart: unless-stopped` sur les trois : ils repartent au démarrage de la
machine et après un plantage.

### Un détail qui compte : l'adresse des clients

Le conteneur applicatif tourne avec `--forwarded-allow-ips` restreint au réseau
interne de la composition (`172.30.0.0/16`), d'où le sous-réseau figé dans
`docker-compose.yml`.

Sans cela, `request.client.host` vaudrait l'adresse de Caddy pour tout le monde,
et la limitation de débit compterait tous les visiteurs sur une seule clé : un
attaquant verrouillerait l'application entière. Avec une plage trop large à
l'inverse, n'importe qui falsifierait son adresse via `X-Forwarded-For`.

## Sauvegardes

Les instantanés arrivent dans `deploy/sauvegardes/`, en `0600`.

Copier le fichier `.db` à chaud serait faux : en mode WAL, les écritures
récentes vivent dans le fichier `-wal` et une copie brute peut attraper un état
incohérent. Le script passe par l'API `backup()` de SQLite, qui produit un
instantané cohérent sans interrompre les écritures.

```bash
# Sauvegarde immédiate
docker compose exec backup python /app/backup.py

# Restauration
docker compose stop app
docker compose run --rm -v "$PWD/sauvegardes:/sauvegardes" app \
  sh -c "cp /sauvegardes/motomatch-XXXX.db /data/motomatch.db"
docker compose start app
```

Ces sauvegardes sont sur la même machine que la base : elles protègent d'une
fausse manœuvre, **pas** d'un disque mort ni d'un vol. Les recopier ailleurs
(`rsync`, disque externe) reste à ta charge.

## Mise à jour

```bash
git pull
docker compose build app
docker compose up -d
```

Le schéma se crée et se complète au démarrage (`init_db`), il n'y a pas de
migration à lancer. Sauvegarde d'abord, par principe.

## Sans Docker

Sur une machine légère type Raspberry Pi, `motomatch.service` et le couple
`motomatch-backup.{service,timer}` font le même travail avec systemd :

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now motomatch motomatch-backup.timer
```

Lire les unités avant : elles supposent le dépôt dans `/opt/motomatch`, un
environnement virtuel dans `/opt/motomatch/.venv` et un utilisateur `motomatch`.

## Vérifier que tout va bien

```bash
curl -sk https://$MOTOMATCH_SITE_ADDRESS/api/health     # {"status":"ok"}
docker compose ps                                       # les 3 services « Up »
ls -lh sauvegardes/                                     # au moins un instantané
```

## Avant d'ouvrir à d'autres personnes

Le kit rend l'hébergement propre, il ne rend pas l'application prête pour un
public. Ce qui manque encore est listé dans
[`../motomatch/SECURITY.md`](../motomatch/SECURITY.md) : pas de vérification
d'adresse e-mail, pas d'envoi ni de modération des photos, pas de file de
traitement des signalements. À garder pour toi et tes proches en l'état.
