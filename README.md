# TikTok stories AI

Projet Windows qui enregistre un épisode Spotify, transcrit la narration avec
WhisperX, génère des illustrations, assemble une vidéo verticale avec FFmpeg,
puis utilise le navigateur pour la publication sur TikTok.

## Installation sur un deuxième PC

Prérequis : Git, Python **3.12**, Google Chrome et les commandes `ffmpeg` et
`ffprobe` accessibles dans le `PATH`. L'enregistrement utilise la sortie audio
Windows : le PC doit avoir une sortie audio fonctionnelle.

Dans PowerShell, depuis le dossier où installer le projet :

```powershell
git clone https://github.com/Awksys/TikTok_stories_AI_v2.git
cd TikTok_stories_AI_v2
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Renseigner sa propre clé `OPENAI_API_KEY` dans `.env`. Les versions de
`requirements.txt` correspondent au PC de référence ; les dépendances de
transcription sont volumineuses. WhisperX télécharge ses modèles au premier
usage.

Se connecter à Spotify et à TikTok sur chaque PC pour créer les sessions locales
dans `Cookies/`. Pour la première connexion Spotify, le réglage `HEADLESS` dans
`spotify_episode_source.py` peut être mis temporairement à `False` afin de voir
le navigateur et de se connecter. Les cookies de session restent hors du dépôt.

La police, les éléments de l'encadré du titre et le son d'introduction sont
inclus dans `Fonts/`, `Illustrator/Final/` et `Sounds/`. Le fichier source
`Illustrator/overlay.ai` est également conservé. Les autres dossiers de travail
sont créés par le programme lorsqu'ils sont nécessaires.

## Passer d'un PC à l'autre

Au début d'une session de travail, depuis la racine du projet :

```powershell
git status
git pull --ff-only
```

Après les modifications, avant de changer de PC :

```powershell
git add .
git commit -m "Description des modifications"
git push
```

Sur l'autre PC, récupérer ensuite les changements avec `git pull --ff-only`.
Le premier commit ou push sur ce PC peut demander de se connecter à GitHub et
de configurer l'identité Git indiquée par Git.

`used_episodes_from_spotify.json` est suivi dans Git pour partager l'historique
des épisodes utilisés. Un enregistrement peut modifier ce fichier : l'inclure
dans le commit avant de changer de PC. Git synchronise lors des `pull` et `push`,
pas en continu. Faire tourner l'automatisation sur un seul PC à la fois pour
éviter de sélectionner simultanément le même épisode.

Si le `pull` refuse de continuer, conserver les modifications locales et les
examiner avec `git status` avant de résoudre le désaccord. Le rapport de
comparaison initial se trouve dans [docs/comparaison_versions_2026-11-04.md](docs/comparaison_versions_2026-11-04.md).

## Lancement

Toujours lancer depuis la racine du projet, car les chemins des ressources
sont relatifs à ce dossier :

```powershell
.\.venv\Scripts\python.exe spotify_stories_to_TT.py
```

Le programme attend l'horaire `TARGET_HOUR` défini dans
`spotify_stories_to_TT.py`. Les réglages du nombre de publications, du compte
et du délai sont dans l'appel à `main_video` du même fichier. Le réglage actuel
`KILL_CHROME_ON_START=True` ferme les processus Chrome au démarrage.

## Vérification du code

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

Les tests simulent les appels de transcription et de génération d'images.
Ils ne lancent pas le cycle de publication.

## Fichiers propres à chaque PC

Git exclut `.env`, les cookies, les environnements Python, les pilotes Chrome,
les journaux, les enregistrements et les médias générés. Les vidéos finales
restent dans `Outputs/` sur le PC qui les a générées. Le fichier
`.env.example` sert uniquement de modèle, avec une valeur fictive.
