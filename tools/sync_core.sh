#!/usr/bin/env bash
# Copie le code partagé (core/) dans android/ : utile pour lancer l'app en local
# (le workflow GitHub fait la même chose avant de compiler). Ces copies sont ignorées par git.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf android/providers
cp -r core/providers android/providers
cp core/pricing.py android/pricing.py
find android/providers -name __pycache__ -prune -exec rm -rf {} \;
echo "core/ -> android/ synchronisé"
