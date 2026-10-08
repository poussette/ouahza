# Mise en place d'Ouahza (une seule fois)

## 0. Avant de commencer
- Dans l'ancienne app « Wallet Checker » : Paramètres → copie ta liste d'adresses (zone « Configuration des wallets ») et colle-la quelque part (Notes…). Ouahza est une nouvelle app : elle ne reprend pas les paramètres de l'ancienne.
- Un compte GitHub (`poussette`), un ordinateur avec `git` et Java (`keytool`).

## 1. Créer le dépôt
GitHub → **New repository** → nom `ouahza`, **Public** (l'app interroge l'API GitHub sans jeton), ne coche aucune option d'initialisation.

## 2. Créer la clé de signature et les secrets (AVANT le premier push)
```
cd ouahza            # le dossier dézippé
bash tools/make_keystore.sh | tee secrets.txt
```
Le script crée `ouahza.jks` et affiche 3 valeurs. Dans GitHub → dépôt `ouahza` → Settings → Secrets and variables → Actions → New repository secret, crée :
- `ANDROID_KEY_ALIAS` = `ouahza`
- `ANDROID_KEYSTORE_PASSWORD` = le mot de passe affiché
- `ANDROID_KEYSTORE_BASE64` = le long texte (une seule ligne)

Sauvegarde `ouahza.jks` et le mot de passe hors du dépôt (gestionnaire de mots de passe), puis supprime `secrets.txt`. Perdre la clé = devoir désinstaller l'app une fois pour en changer.

## 3. Pousser le code
```
git init -b main
git add .
git commit -m "Ouahza v0.1.0"
git remote add origin https://github.com/poussette/ouahza.git
git push -u origin main
```
(`.gitignore` exclut déjà `*.jks` et `secrets.txt`.) Sans git en ligne de commande : sur la page du dépôt vide, « uploading an existing file », puis glisse tout le contenu du dossier **y compris le dossier caché `.github`**.

## 4. Première compilation
Onglet **Actions** du dépôt : « Build Ouahza APK » démarre (tests → build ×2 → release, 15-25 min la première fois). Quand c'est vert, la page **Releases** contient `v0.1.0` avec `Ouahza-0.1.0-universal.apk` et `Ouahza-0.1.0-arm64.apk`.

Si la Release n'apparaît pas : l'avertissement « Secrets ANDROID_KEYSTORE_* absents » dans les logs veut dire que l'étape 2 n'a pas été faite avant le push ; crée les secrets puis relance (Actions → Re-run all jobs).

## 5. Installer sur le téléphone
Télécharge `Ouahza-0.1.0-universal.apk` depuis la Release, installe-le (autorise la source si Android le demande), puis Paramètres → colle ta configuration. Tu peux ensuite désinstaller « Wallet Checker ».
Utilise toujours la variante **universal** : c'est celle que l'app propose lors des mises à jour.

## 6. Mises à jour suivantes
Voir `README.md` (« Publier une nouvelle version »). Pour tester le circuit : `python3 tools/set_version.py 0.1.1`, commit, push ; une fois la Release `v0.1.1` publiée, ouvre Ouahza 0.1.0 : la fenêtre « Mise à jour disponible » apparaît.

## 7. Ranger l'ancien dépôt
Quand tout fonctionne : `wallet_checker_apk` → Settings → Archive this repository.
