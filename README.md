# Ouahza

Suivi de portefeuilles crypto (BTC / ETH / SOL / MultiversX) : soldes, tokens, staking, valorisation en € / $
(y compris les LP tokens MultiversX lus directement dans les contrats des pools).

```
core/      code partagé (providers/, pricing.py) — une seule copie pour le CLI et l'app
cli/       version ligne de commande (python3 cli/main.py addresses.txt) + tests
android/   app Android (Kivy), compilée par GitHub Actions
tools/     set_version.py (changer la version), make_keystore.sh (clé de signature), sync_core.sh
```

## Au quotidien

**Lancer le CLI** : `cd cli && python3 main.py addresses.txt` (voir `cli/README.md`).
**Tests** : `python3 -m unittest discover -s cli/tests`.

**Publier une nouvelle version de l'app**
1. `python3 tools/set_version.py 0.1.1` (change le numéro partout ; il doit être plus grand que le précédent).
2. Mets à jour `android/release_notes.txt` (texte montré dans la fenêtre de mise à jour de l'app).
3. `git add -A && git commit -m "v0.1.1" && git push`.
4. GitHub Actions lance les tests, compile et signe les APK, puis publie la Release `v0.1.1`.
5. Au prochain lancement, l'app du téléphone propose la mise à jour (paramètres conservés).

Mise en place initiale (nouveau dépôt, clé de signature, premier APK) : `MISE_EN_PLACE.md`.
