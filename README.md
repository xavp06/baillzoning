# baillzoning

Pilotage d'un système de climatisation / chauffage BaillZoning depuis Home Assistant, sans la passerelle cloud BaillConnect.

Le service lit le bus RS-485 (via une passerelle Waveshare en TCP), publie l'état sur MQTT avec la découverte Home Assistant, et émule la passerelle Modbus (esclave 10) pour que l'unité centrale accepte les commandes envoyées depuis HA.

## Fonctionnement

- **Lecture** : décodage des trames, image des registres, publication MQTT (`<prefix>/thermostat/thN/…`, `<prefix>/uc/…`, `<prefix>/intesis/…`).
- **Émulateur** : répond aux requêtes de l'UC adressées à l'esclave 10. Il n'émet jamais spontanément.
- **Commandes** : consigne, préréglage, marche des thermostats et mode général. Une commande à la fois, confirmée par l'UC avant la suivante. Le statut (`en_attente`, `en_cours`, `appliquee`, `echec`, `refusee`) est publié dans les attributs du climate.
- **Découverte HA** : entités `climate` (une par thermostat) et `select` (mode général). Aucune intégration personnalisée nécessaire.

## Prérequis

- Une passerelle RS-485 / Ethernet configurée en TCP server transparent (9600 8N2, multi-hôte désactivé).
- Un broker MQTT joignable.
- Docker sur une machine du réseau local (testé sur Raspberry Pi).

## Sécurité

- **Ne pas exposer le broker MQTT hors du réseau local** sans authentification. Le service accepte `MQTT_USER` et `MQTT_PASSWORD`.
- Une seule instance doit tourner. Deux répondeurs sur le même bus provoquent des conflits.
- Pour une lecture seule, mettre `EMU_RESPOND=0`. Le service ne répond alors plus à l'UC.
- Les commandes ne sont envoyées qu'aux thermostats listés dans `VERIFIED_THERMOSTATS`. Ajouter un thermostat à cette liste doit se faire après un test sur le bus.

## Configuration

Copier `.env.example` en `.env` et l'adapter. Les variables principales :

| Variable | Rôle |
|---|---|
| `WAVESHARE_HOST`, `WAVESHARE_PORT` | Adresse de la passerelle RS-485 (obligatoire pour `WAVESHARE_HOST`) |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_PREFIX` | Broker et préfixe des topics |
| `MQTT_USER`, `MQTT_PASSWORD` | Identifiants MQTT (optionnels) |
| `EMU_RESPOND` | `1` pour émuler la passerelle 10, `0` pour lecture seule |
| `HOT_MIN`, `HOT_MAX`, `COLD_MIN`, `COLD_MAX` | Bornes des consignes par mode (défaut 16–30 °C) |
| `THERMOSTAT_COUNT` | Nombre de thermostats (défaut 5). Au-delà de 5, voir les limites ci-dessous. |
| `THERMOSTAT_NAMES` | Noms affichés dans HA, au format `1:Salon,2:Chambre`. Un numéro absent s'appelle « Thermostat N ». |
| `VERIFIED_THERMOSTATS` | Thermostats autorisés aux commandes, par numéro (`1,2,3`). Vide par défaut : aucune commande. |
| `JOURNAL_DIR` | Dossier du journal des trames et des changements de registres. Vide par défaut : journal désactivé. Il grossit à chaque trame, sans rotation. Ne l'activer que pour une analyse. |
| `EMU_STATE` | Fichier d'état de l'émulateur. Défaut : `data/emulator_state.json`. Inutile de le définir dans Docker, le volume `data/` le conserve. |
| `INTESIS_ERRORS_FILE` | Fichier JSON local `{"code": ["nom", "description"]}` pour les libellés des erreurs Intesis. Non fourni dans le dépôt (voir ci-dessous). Sans lui, seul le numéro d'erreur est publié. |
| `PROFILE` | Profil de carte du bus : nom d'un fichier de `profiles/` (défaut `baillzoning-uc82`) ou chemin vers un `.json`. Un profil invalide empêche le démarrage. |

## Profils de bus

Les adresses Modbus, les codes de mode et de préréglage, les unités et les temporisations ne sont pas dans le code : ils viennent d'un profil JSON, dans `profiles/`.

- `profiles/schema.json` décrit le format d'un profil.
- `profiles/baillzoning-uc82.json` est le profil de l'installation testée (UC firmware 82, Intesis DK-RC-MBS-1, 5 thermostats).
- Chaque bloc a un niveau de confiance : `validated` (observé sur le bus) ou `hypothesis`. Une commande vers un bloc `hypothesis` est refusée, même si le thermostat est dans `VERIFIED_THERMOSTATS`.

Une autre passerelle ou un autre firmware demande un nouveau profil, établi à partir de captures sur le bus. Le mode diagnostic n'est pas encore disponible : un profil non validé ne doit servir qu'à observer, avec `EMU_RESPOND=0`.

Ce qui reste dans le code : la logique de séquencement des commandes, les règles de sécurité, la correspondance des noms de préréglage vers Home Assistant (`comfort`/`eco`) et les définitions des entités HA.

## Tests

```sh
python3 -m unittest discover -s tests -t . -v
PYTHONPATH=. python3 tools/regression_check.py <frames.jsonl> --record ref.json      # avant une modification
PYTHONPATH=. python3 tools/regression_check.py <frames.jsonl> --compare ref.json     # après une modification
```

- `tests/` : tests unitaires (profil, file de commandes). Ils ne demandent aucune donnée.
- `tools/regression_check.py` : rejoue un journal de trames capturées (`frames.jsonl`) et vérifie que l'émulateur répond à l'identique et que l'état publié ne change pas. Le journal n'est pas dans le repo, car il décrit l'installation. Sans ce journal, l'outil ne sert à rien.

Ces fichiers ne sont pas copiés dans l'image Docker.

## Déploiement avec Docker

Les fichiers `.example` sont des modèles. Les copier avant utilisation :

```sh
cp .env.example .env                                  # puis adapter les valeurs
cp docker-compose.yml.example docker-compose.yml
docker compose up -d --build
docker compose logs -f
```

Sur une installation avec Portainer, les variables se règlent directement dans la stack, et le fichier compose n'est pas utilisé.

Le dossier `data/` contient l'état de l'émulateur (`emulator_state.json`). Il est conservé d'un redémarrage à l'autre.

Pour une mise à jour : `git pull`, puis `docker compose up -d --build`. L'émulateur ne répond pas à l'UC pendant une dizaine de secondes pendant le redémarrage.

Avec Portainer (cas de l'installation d'origine) : `git pull` et `docker build -t baillzoning:latest .` sur la machine hôte, puis **Recreate** du conteneur, sans « pull ».

## Limites connues

- Les adresses du bus sont vérifiées pour cinq thermostats. Un sixième utiliserait le bloc 116 et la commande 60 à 69, qui n'ont jamais été observés.
- Le mode Ventilation n'est pas disponible avec le firmware UC 82 (il faut au moins 83).
- Le registre de marche de l'Intesis ne suit pas toujours le mode envoyé. Le retour fiable du mode est `hold[8]`.

## Erreurs Intesis

Les libellés des codes d'erreur de la passerelle Intesis DK-RC-MBS-1 ne sont pas inclus : ils reproduisent le manuel du fabricant, dont le droit d'auteur interdit la reproduction. Pour les afficher, consulter le manuel, puis écrire un fichier JSON local `{"code": ["nom", "description"]}` et définir `INTESIS_ERRORS_FILE` vers ce fichier.

## Licence

Ce projet est distribué sous la **GNU General Public License v3.0** (fichier `LICENSE`). Toute version modifiée distribuée doit rester sous GPL-3.0, avec son code source.

Ce projet n'est affilié ni à BaillZoning, ni à BaillConnect, ni à Intesis, ni à Daikin. Les noms de produits appartiennent à leurs titulaires.

## Avertissement

Ce projet écrit sur le bus d'une installation de chauffage et de climatisation. Une mauvaise écriture peut modifier les consignes d'une pièce ou couper le chauffage de toute la maison. Tester les commandes avec précaution, et ne pas modifier le code sans validation sur le matériel.
