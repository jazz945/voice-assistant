# Image de production de MotoMatch.
#
# À construire depuis la racine du dépôt :
#   docker build -f deploy/Dockerfile -t motomatch .

FROM python:3.12-slim AS base

# - PYTHONDONTWRITEBYTECODE : pas de .pyc dans un conteneur éphémère
# - PYTHONUNBUFFERED : les journaux sortent immédiatement, sans tampon
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# argon2-cffi fournit des roues précompilées ; pas de chaîne de compilation à
# installer, ce qui garde l'image petite et réduit la surface d'attaque.
COPY motomatch/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY motomatch/ ./motomatch/

# Utilisateur non privilégié : une faille dans l'application ne donne pas root.
# Le répertoire de données lui appartient, c'est le seul endroit inscriptible.
RUN useradd --system --create-home --uid 10001 motomatch \
    && mkdir -p /data \
    && chown -R motomatch:motomatch /data /app
USER motomatch

ENV MOTOMATCH_DB=/data/motomatch.db \
    FORWARDED_ALLOW_IPS=127.0.0.1

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# Forme shell pour que $FORWARDED_ALLOW_IPS soit développé.
#
# `--proxy-headers` avec `--forwarded-allow-ips` restreint aux adresses du
# reverse proxy est indispensable : sans lui, `request.client.host` vaut
# l'adresse du proxy pour tout le monde, et la limitation de débit compterait
# tous les visiteurs sur une seule clé — un seul attaquant verrouillerait
# l'application entière. Avec un `--forwarded-allow-ips` trop large en
# revanche, n'importe qui falsifie son IP via `X-Forwarded-For` : d'où la
# restriction au seul réseau du proxy.
CMD uvicorn motomatch.main:app \
    --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips="$FORWARDED_ALLOW_IPS" \
    --workers 1 \
    --no-server-header
