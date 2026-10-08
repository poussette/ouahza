# Ouahza (CLI)

Vérifie le solde (coin natif + tokens) d'une liste d'adresses Bitcoin,
Ethereum, Solana et MultiversX, à partir d'un simple fichier texte.

Conçu pour être facilement étendu à d'autres blockchains.

## Installation

```bash
pip install -r requirements.txt
```

## Utilisation

```bash
python main.py addresses.txt
```

Chaque coin natif, token et position de staking est valorisé en **USD et
EUR** (via l'API gratuite CoinGecko, sans clé requise), et le total par
wallet est affiché et exporté. Pour désactiver la valorisation (usage hors
ligne, ou éviter le rate-limit CoinGecko sur une grosse liste) :

```bash
python main.py addresses.txt --no-price
```

**Par défaut, seules les lignes valant au moins 1 cent USD (0,01 $) sont
affichées/exportées** : sont masqués (1) les tokens/positions sans valeur
connue (tokens illiquides sans prix de marché, NFT/SFT — pas de source de
floor price intégrée), puisqu'une ligne sans valeur ne peut de toute façon
pas entrer dans un total, et (2) les tokens/positions valorisés **moins de
0,01 $** (poussière), ainsi que la ligne du coin natif lui-même quand son
montant vaut moins de 0,01 $ (un coin natif sans prix connu reste affiché).
Rien n'est perdu dans les données sous-jacentes, seulement dans ce qui
s'affiche : les totaux par wallet et par label incluent toujours tout, et si
des lignes sont masquées, un message l'indique sous chaque wallet concerné
avec leur nombre.

Deux options pour en voir plus :

```bash
# aussi les lignes valorisées mais < 0,01 $ (poussière)
python main.py addresses.txt --show-dust

# liste complète : en plus, celles sans valeur connue (implique --show-dust)
python main.py addresses.txt --show-unpriced
```

Avec `--no-price`, ce filtrage n'aurait aucun sens (rien n'a de valeur,
donc tout serait masqué) : `--show-unpriced` est donc automatiquement
activé dans ce cas, pas besoin de le préciser.

Fichier d'entrée (voir `addresses.example.txt`) : une adresse par ligne.
- `bitcoin,bc1q...` force la blockchain
- `bc1q...` seule : la blockchain est auto-détectée d'après le format
- lignes vides et lignes commençant par `#` sont ignorées
- `[Un Label]` regroupe toutes les adresses qui suivent (jusqu'au prochain
  `[Label]` ou la fin du fichier) sous ce nom : chaque wallet reste
  valorisé individuellement, mais un **total combiné par label** est aussi
  calculé et affiché après le rapport par wallet (et exporté en JSON/CSV).
  Le même label peut réapparaître plus loin dans le fichier pour lui
  ajouter d'autres adresses. Les adresses placées avant le premier `[Label]`
  restent non groupées (valorisées individuellement, sans total combiné).

  ```
  [Perso]
  bitcoin,bc1q...
  ethereum,0x...

  [Trading]
  solana,...
  erd1...
  ```

Export des résultats :

```bash
python main.py addresses.txt --output resultats.json
python main.py addresses.txt --output resultats.csv --format csv
```

Le JSON exporté a la forme `{"wallets": [...], "label_totals": {...}}` ; le
CSV a une colonne `label` par ligne, plus un petit bloc `LABEL_TOTAL` à la
fin du fichier récapitulant chaque groupe. Les exports suivent le même
filtrage que l'écran : un coin natif masqué (< 0,01 $) garde ses valeurs
dans le JSON mais y est marqué `"native_hidden": true`, et sa ligne `native`
est omise du CSV.

Options utiles :
- `--workers N` : nombre de requêtes en parallèle (défaut : 6)

## Clés API (optionnelles)

Toutes les blockchains fonctionnent sans clé API pour le solde natif.
Pour lister les tokens ERC-20 d'une adresse Ethereum, il faut une clé
gratuite Etherscan (https://etherscan.io/apis) :

```bash
export ETHERSCAN_API_KEY="votre_clé"
```

Sans cette clé, le solde ETH natif est quand même récupéré, mais les
tokens ERC-20 sont ignorés avec un avertissement.

Le staking beacon chain Ethereum (`beacon-stake`) fonctionne sans clé via
l'API publique beaconcha.in, mais on peut fournir une clé gratuite pour un
quota plus généreux :

```bash
export BEACONCHAIN_API_KEY="votre_clé"
```

Bitcoin, Solana et MultiversX n'ont besoin d'aucune clé.

## Sources de données

| Chaîne      | API                                  | Clé requise |
|-------------|---------------------------------------|-------------|
| Bitcoin     | blockstream.info (Esplora)            | non |
| Ethereum    | RPC public (solde) + Etherscan (tokens) | non / oui pour les tokens |
| Solana      | RPC public mainnet-beta               | non |
| MultiversX  | api.multiversx.com                    | non |
| Prix natifs (BTC/ETH/SOL/EGLD) | CoinGecko (`/simple/price`) | non |
| Prix tokens ERC-20 / SPL | CoinGecko (`/simple/token_price/{platform}`) | non |
| Prix tokens ESDT MultiversX | xExchange, via `api.multiversx.com/mex-tokens` (large) + `/tokens?identifiers=` (ciblé, tout token avec liquidité DEX) | non |
| Taux de change USD→EUR (pour les prix xExchange, donnés en USD) | frankfurter.app (BCE) | non |

## Architecture

```
core/                        # code partagé CLI + Android (une seule copie)
├── pricing.py               # valorisation USD/EUR
└── providers/
    ├── base.py              # interface BaseProvider + WalletBalance/TokenBalance
    ├── __init__.py          # registre des providers + détection auto de chaîne
    ├── bitcoin.py / ethereum.py / solana.py / multiversx.py
    ├── lp.py                # valorisation des LP tokens via les contrats
    ├── updater.py           # recherche de mise à jour (utilisé par l'app)
    └── net.py / safe.py / version.py
cli/
├── main.py                  # CLI : lecture du fichier, orchestration, sortie
├── lp_probe.py              # diagnostic d'un LP token
├── tests/
├── addresses.example.txt
└── requirements.txt
```

## Ajouter une nouvelle blockchain

1. Créer `core/providers/<chaine>.py` avec une classe héritant de `BaseProvider` :
   - `chain_id`, `display_name`, `native_symbol`
   - `matches(address)` : détection du format d'adresse
   - `get_balance(address)` : retourne un `WalletBalance`
2. L'importer et l'ajouter à la liste `PROVIDERS` dans `providers/__init__.py`.

C'est tout : `main.py` n'a pas besoin d'être modifié.

## Staking / positions hors du wallet

Une partie des avoirs n'est pas détenue directement par l'adresse mais par
des smart contracts (staking, delegation...). Ces positions sont incluses
et identifiables via le champ `asset_type` :

- **MultiversX** :
  - `delegation` : EGLD délégué à un pool de staking
  - `delegation-rewards` : récompenses de délégation réclamables
  - `delegation-unbonding` : EGLD undelegated mais encore en période de
    cooldown (~10 jours) avant de pouvoir être retiré — le nom indique le
    nombre de jours restants estimé. Une tranche dont le cooldown est déjà
    à 0 (terminé) n'est **pas** remontée ici pour éviter un double comptage
    avec `delegation-unbondable` ci-dessous (l'API MultiversX garde parfois
    l'entrée dans les deux champs une fois le cooldown écoulé)
  - `delegation-unbondable` : EGLD undelegated dont le cooldown est terminé,
    prêt pour une transaction de retrait (`withdraw`)
  - `delegation-legacy` : ancien système de délégation
  - `delegation-legacy-unbonding` : équivalent unbonding pour l'ancien
    système (support best-effort, champ API moins documenté)
  - `validator-stake` : EGLD staké en faisant tourner son propre nœud validateur
- **Solana** :
  - `staked` : SOL actuellement délégué (actif ou en cours d'activation) via
    le Stake Program natif — un compte de stake par délégation
  - `stake-withdrawable` : SOL dans un compte de stake qui n'est **plus**
    activement délégué (désactivation en cours ou terminée) — ce n'est plus
    du staking actif, mais le SOL n'est pas non plus revenu dans le wallet ;
    il est en attente d'un retrait (`withdraw`)
  - les comptes de stake sont recherchés par autorité "staker" (celle qui
    contrôle la délégation) uniquement — l'autorité "withdrawer" (retrait)
    n'a plus besoin de correspondre aussi. De nombreux setups légitimes
    utilisent une autorité de retrait différente (ex : clé hardware séparée
    du wallet chaud utilisé pour gérer la délégation) ; l'exiger en plus
    faisait disparaître à tort ces positions réelles. Le nom de chaque
    position de stake indique l'autorité de retrait quand elle diffère de
    l'adresse, pour vérification
  - le montant remonté est le **solde réel actuel** du compte de stake, pas
    le champ `delegation.stake` : ce dernier peut rester figé sur l'ancien
    montant délégué même après un retrait complet. Un compte totalement
    déstaké-et-retiré reste généralement ouvert on-chain avec seulement sa
    réserve "rent-exempt" (~0.0023 SOL) ; ces comptes vidés sont filtrés
    (seuil de poussière à 0.005 SOL) pour ne pas apparaître comme une
    position réelle
  - les dérivés de liquid staking (mSOL, jitoSOL, stSOL...) apparaissent
    déjà comme tokens SPL classiques (`asset_type: token`), puisqu'ils sont
    directement détenus dans le wallet.
- **Ethereum** :
  - `beacon-stake` : ETH staké nativement sur la beacon chain (32 ETH par
    validateur), récupéré via l'API publique beaconcha.in en associant
    l'adresse (déposant ou credentials de retrait 0x01) à ses validateurs
  - les dérivés de liquid staking (stETH, rETH, cbETH...) sont déjà des
    ERC-20 classiques et apparaissent en `asset_type: token` (nécessite
    `ETHERSCAN_API_KEY`)

`asset_type` par chaîne, valeurs possibles :

| Chaîne      | Types possibles |
|-------------|------------------|
| Bitcoin     | (aucun token/position) |
| Ethereum    | `token` (ERC-20), `beacon-stake` (validateur beacon chain) |
| Solana      | `token` (SPL), `staked` (délégation active), `stake-withdrawable` (déjà désactivé, en attente de retrait) |
| MultiversX  | `esdt`, `meta-esdt`, `nft`, `sft`, `delegation`, `delegation-rewards`, `delegation-unbonding`, `delegation-unbondable`, `delegation-legacy`, `delegation-legacy-unbonding`, `validator-stake` |

## Limites connues

- Solana : le mapping symbole/nom des tokens SPL dépend de la liste
  publique Jupiter ; un mint inconnu s'affiche avec son adresse.
  `getProgramAccounts` (utilisé pour le staking) est parfois désactivé ou
  limité sur les RPC publics gratuits ; en cas d'échec, un warning est
  renvoyé plutôt qu'une erreur bloquante. Les récompenses de staking Solana
  ne sont **pas** créditées séparément : elles s'ajoutent directement au
  solde du compte de stake (`delegation.stake`) à chaque epoch. Le script
  ne peut donc pas isoler un montant "gains" comme le ferait un explorateur
  qui suit l'historique — seul le montant actuellement staké/retirable est
  remonté, principal et récompenses compoundées confondus.
  Les comptes de stake sont recherchés par autorité "staker" uniquement
  (pas "withdrawer") : si cette adresse sert aussi d'autorité de délégation
  pour un pool/service gérant le stake d'autres personnes, leurs comptes
  seraient également remontés à tort. Il n'existe pas de moyen fiable à
  100% de distinguer ce cas depuis les seules données on-chain publiques ;
  vérifiez l'autorité de retrait affichée dans le nom de chaque position si
  le total semble anormalement élevé.
- Les API publiques gratuites sont rate-limitées : pour de gros volumes
  d'adresses, réduire `--workers` ou ajouter vos propres clés/endpoints
  (RPC dédiés, Etherscan Pro, etc.).
- Bitcoin : pas de notion de "token" native (BRC-20/Ordinals non couverts).
- Ethereum : le restaking (EigenLayer, etc.) n'est pas couvert, seul le
  staking beacon chain "classique" l'est ; beaconcha.in est aussi rate-limité
  côté gratuit (`BEACONCHAIN_API_KEY` optionnelle pour un quota plus élevé,
  clé gratuite sur https://beaconcha.in/pricing).
- Valorisation : les NFT/SFT ne sont pas valorisés (pas de source de floor
  price fiable intégrée). Les tokens ESDT/MetaESDT MultiversX sont valorisés
  via les prix xExchange (par `api.multiversx.com/mex-tokens`) : un token
  sans paire de liquidité sur xExchange reste non valorisé, comme pour les
  autres chaînes. Les API publiques utilisées (CoinGecko, MultiversX,
  frankfurter.app) sont rate-limitées : sur une longue liste d'adresses avec
  beaucoup de tokens différents, la valorisation peut être ralentie ou
  partiellement manquante ; relancer plus tard ou utiliser `--no-price` si
  besoin.

## LP tokens (MultiversX)

Les LP tokens des DEX autres que xExchange n'ont pas de prix public. L'outil les valorise en lisant le **contrat de la pool** (requête `vm-values/query` sur une passerelle MultiversX) : `prix du LP = valeur des réserves ÷ quantité totale de LP`.

- **Nœud personnalisé** : `--mvx-gateway https://mon.noeud` (CLI), variable `MULTIVERSX_GATEWAY_URL`, ou le champ « Gateway API MultiversX » de l’app. Vide = passerelle publique `https://gateway.multiversx.com`. Seul `https://` est accepté. `--no-lp` (ou la case de l'app) désactive la fonction.
- **Trouver la pool** : l'émetteur d'un LP est souvent un routeur/une factory, pas la pool. On interroge donc `/tokens/<LP>/roles` : le contrat qui détient les droits de mint/burn du LP est la pool (puis l'émetteur en dernier recours). Aucun code propre à un DEX pour cette étape.
- **Lire la pool** : un contrat ne publie pas son ABI. Chaque adaptateur (`ADAPTERS` dans `providers/lp.py`) liste des *noms de vues candidats* : `xexchange-pair`, `jex-pair`, `onedex` (un seul contrat, vues indexées par un id de paire), `list-pool` (AshSwap et pools à liste de jetons ; sans vue de réserves, les soldes du contrat servent de réserves), `jex-stable` (pools stables JEX : jetons lus dans `getStatus`, réserves = soldes du contrat). Pour les pools stables, le prix obtenu doit **concorder (±12 %) avec le `getVirtualPrice` publié par la pool**, sinon la ligne reste non valorisée.
- **Garde-fous** : un résultat n'est retenu que si la pool **nomme elle-même ce LP** (obligatoire), chaque réserve est ≤ au solde réel du contrat, et la quantité totale de la pool concorde avec `minted - burnt`. Une mauvaise hypothèse donne « non valorisé », jamais une valeur fausse.
- **Valorisation** : si tous les jetons de la pool ont un prix, on additionne ; si un seul des deux (pool 50/50 à produit constant), on double et la ligne indique « estimation 50/50 » ; pools stables / à plus de 2 jetons : tous les jetons doivent avoir un prix. Les pools stables sans vue de réserves utilisent les soldes du contrat, qui peuvent inclure des frais non distribués : écart de quelques % possible.
- **Un DEX n'est pas reconnu ?** `python lp_probe.py <LP-id ou TICKER>` affiche les contrats candidats et les vues qui répondent. Ajoutez ensuite un dictionnaire à `ADAPTERS` (données seulement), ou envoyez la sortie pour qu'on l'écrive. Non géré à ce jour : la découverte automatique des vues à partir du bytecode.
- **Cache et reprise** : l'outil mémorise, pour chaque LP, quel contrat est la pool et quelle famille de vues la lit (fichier `lp_cache.json`, droits 0600, données publiques uniquement : identifiants de jetons et adresses de contrats, jamais vos adresses ni un prix). Les valeurs sont **recalculées et revérifiées sur la chaîne à chaque lancement**. Les LP illisibles sont ignorés 24 h pour ne pas gaspiller le budget. Si le budget d'une passe est épuisé, jusqu'à 3 passes s'enchaînent dans la même actualisation ; sinon relancez : les LP déjà connus coûtent peu. CLI : `--lp-cache FICHIER` (défaut `~/.ouahza/lp_cache.json`, `none` pour désactiver). APK : fichier dans le dossier privé de l'appli.
- **Contrat non vérifié ?** Un code hash inconnu n'est pas une erreur : c'est un contrat que l'outil n'a jamais vu. Comme n'importe qui peut déployer un contrat imitant une pool, l'outil ne s'auto-approuve pas. Deux façons de lever la mention sans me renvoyer chaque hash : (1) `python3 lp_probe.py <LP-id> --trust` après avoir vérifié que c'est bien une pool du DEX : il mémorise le code hash (par adaptateur) dans `lp_cache.json` et toutes les pools au même code deviennent vérifiées (ligne « hash approuvé ») ; (2) le **mode permissif** (CLI `--lp-trust-all`, case dans les paramètres de l'app) : toute pool qui passe les contrôles de cohérence compte comme vérifiée, y compris l'estimation 50/50 (ligne « mode permissif »). Moins sûr, à réserver à un usage personnel.
- **Limites** : budget par passe de 1000 requêtes, 120 s, 80 LP par actualisation ; le prix des jetons de la pool vient toujours de xExchange. **Contrats non reconnus** : n'importe qui peut déployer un contrat qui répond « comme une pool » avec des chiffres inventés. Seuls les contrats dont le *code hash* a été observé sur les vraies pools du DEX (`code_hashes` dans `ADAPTERS`) sont « vérifiés » ; un autre contrat cohérent est valorisé seulement si **tous** ses jetons ont un prix, jamais par doublement 50/50, et la ligne indique « contrat non vérifié ». Une position supérieure à la quantité totale du LP, ou à 50 M$, n'est jamais valorisée. Gardez un œil critique sur ces lignes : c'est une estimation tirée de la chaîne, pas un prix de marché.

## Versions

Chaque fichier porte son numéro (`__version__`) et `providers/version.py` fixe la release. Après avoir copié un patch, vérifiez que rien n'est resté ancien :

- CLI : `python3 main.py --version` liste tous les composants et signale `<-- DIFFERENT` ceux qui ne correspondent pas ; `python3 lp_probe.py <LP>` affiche la même synthèse en tête.
- App : le numéro en haut affiche `(!)` et la barre d'état nomme les fichiers d'une autre version.


## Sécurité

**Ce qui est protégé** (audit v0.5) :
- Toutes les données reçues des API sont traitées comme hostiles (tokens airdrop avec noms piégés, décimales absurdes, montants NaN/inf, réponses géantes) : texte nettoyé (séquences d'échappement, caractères bidi/invisibles), nombres bornés, taille de réponse et nombre de pages plafonnés.
- Les clés API ne fuient plus dans les messages d'erreur, les exports ou le presse-papier (les erreurs `requests` contiennent l'URL complète, donc la clé : elles sont masquées).
- HTTPS uniquement (un RPC personnalisé en `http://` est ignoré, hors `localhost`) ; la vérification TLS n'est jamais désactivée ; les redirections vers HTTP sont refusées.
- Les adresses sont validées (même quand la chaîne est forcée) et encodées avant d'entrer dans une URL.
- Les exports CSV neutralisent l'injection de formules (`=`, `+`, `-`, `@`) ; les fichiers de sortie du CLI sont créés en `0600`.
- Un token dont la valeur dépasse 1 milliard de $ est considéré comme un artefact de prix et laissé non valorisé.

**À savoir (risques résiduels)** :
- Ta liste d'adresses et tes clés sont stockées en clair dans le stockage privé de l'app (isolé des autres apps, hors sauvegardes). Un téléphone rooté ou déverrouillé y accède.
- Les services tiers (Blockstream, CoinGecko, RPC publics...) voient tes adresses et ton IP. Utilise ton propre RPC si c'est un sujet.
- Un faux token airdroppé dans un pool très peu liquide peut afficher une valeur gonflée mais < 1 Md$ : méfie-toi des lignes de tokens inconnus.
- L'APK est signé avec une clé *debug* générée à chaque build : Android considère chaque build comme un éditeur différent (désinstalle avant de réinstaller). Ne distribue pas ce fichier.
- Ne commite jamais ta vraie liste : `addresses.txt`, `wallets*.txt` et les exports sont dans `.gitignore`. Vérifie avec `git ls-files`.
