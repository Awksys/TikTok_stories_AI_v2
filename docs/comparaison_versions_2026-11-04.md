# Comparaison des copies du 4 novembre 2026

La comparaison porte sur les huit scripts Python, les trois fichiers de tests
et l'historique des épisodes. Le contenu a été comparé directement ; les dates
des fichiers n'ont pas servi à choisir une version.

| Fichier | Copie de référence de ce PC | Copie de l'autre PC dans le ZIP |
| --- | --- | --- |
| `get_subtitles.py` | Limite les sous-titres du titre à la disparition de l'encadré | Correction absente |
| `tests/test_get_subtitles.py` | Contient le test de cette transition | Test absent |
| `spotify_stories_to_TT.py` | `has_run_today = False` au lancement | `has_run_today = True` au lancement |
| `used_episodes_from_spotify.json` | 287 titres, dont les 280 de l'autre copie | 280 titres |

Les huit autres fichiers Python et de tests sont identiques. Aucun script
n'existe uniquement dans l'autre copie. L'historique de ce PC contient tous
les titres de l'autre copie et sept titres supplémentaires.

La base retenue est donc la copie de ce PC, sans remplacement des scripts ni
fusion nécessaire de l'historique. Le réglage `has_run_today=False` permet
d'exécuter les tâches dès le jour du lancement si l'horaire est atteint ;
`True` les considère déjà effectuées jusqu'à la prochaine remise à zéro
prévue par le programme.

Les clés `.env`, les cookies, les journaux et le code Python compilé n'ont pas
été importés depuis le ZIP. Les médias et ressources graphiques n'étaient
pas inclus dans ce ZIP et n'ont donc pas été comparés.
