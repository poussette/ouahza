#!/usr/bin/env bash
# À lancer UNE SEULE FOIS sur ton ordinateur (nécessite Java : commande `keytool`).
# Crée la clé qui signera toutes les futures versions de l'app. Android n'installe
# une mise à jour par-dessus l'app installée (et ne garde donc ses paramètres) que si
# elle est signée par la MÊME clé : sauvegarde bien ouahza.jks et le mot de passe.
set -euo pipefail
command -v keytool >/dev/null || { echo "keytool introuvable : installe Java (ex. sudo apt install default-jre-headless)"; exit 1; }
KS=ouahza.jks
ALIAS=ouahza
[ -e "$KS" ] && { echo "$KS existe déjà : ne l'écrase pas."; exit 1; }
PASS=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
keytool -genkeypair -v -keystore "$KS" -alias "$ALIAS" -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass "$PASS" -keypass "$PASS" -dname "CN=Ouahza, O=Perso, C=FR" >/dev/null
chmod 600 "$KS"
echo
echo "Clé créée : $KS   (ne la mets PAS dans le dépôt GitHub)"
echo
echo "Dans GitHub : dépôt > Settings > Secrets and variables > Actions > New repository secret"
echo "Crée ces 3 secrets :"
echo
echo "  ANDROID_KEY_ALIAS          = $ALIAS"
echo "  ANDROID_KEYSTORE_PASSWORD  = $PASS"
echo "  ANDROID_KEYSTORE_BASE64    = (le long texte ci-dessous, sur une seule ligne)"
echo
base64 -w0 "$KS" 2>/dev/null || base64 "$KS" | tr -d '\n'
echo
echo
echo "Sauvegarde $KS et le mot de passe (gestionnaire de mots de passe) : si tu les perds, il faudra désinstaller l'app une dernière fois."
