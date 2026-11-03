# Diagnostics des transcriptions rejetees

Une sauvegarde est creee ici lorsqu'une transcription echoue a la validation.
Les cinq derniers rejets sont conserves automatiquement. Les archives gerees les
plus anciennes sont supprimees apres la reussite d'une nouvelle sauvegarde.

Chaque dossier date contient :

- `audio.mp3` (ou l'extension du fichier source) : copie exacte de l'audio transcrit ;
- `transcription.json` : resultat WhisperX avant l'alignement ;
- `alignment.json` : resultat complet apres l'alignement, avec les mots et timestamps ;
- `subtitles.ass` : sous-titres produits avant le rejet ;
- `metadata.json` : titre, motifs du rejet, date UTC et reglages utilises.

Ces fichiers sont conserves hors de `Downloads` et survivent donc a son nettoyage.
La sauvegarde ne declenche aucune nouvelle tentative de transcription et ne change
pas le rejet de la video : le workflow passe ensuite a un autre episode Spotify.

Le nombre d'archives se regle avec `MAX_SAVED_TRANSCRIPTION_FAILURES` dans
`transcription_archive.py`. Les dossiers contenant des fichiers ajoutes manuellement
ne sont pas supprimes par le nettoyage automatique.
