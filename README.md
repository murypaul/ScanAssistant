# ScanAssistant

*[Version française plus bas](#français) · [French version below](#français)*

A desktop assistant for digitization capture workflows. It watches a folder
for incoming RAW files, ingests and renames them against an inventory list,
detects and applies a crop automatically, generates the derivative files,
writes metadata, and keeps a full audit trail — so the person at the camera
only has to handle the physical object and check the result.

Built for heritage digitization campaigns (negatives, prints, and other
flat objects) run by a single operator over long, interruptible sessions.

## What it does

- **Folder watching** — detects new RAW files as soon as the camera/transfer
  tool writes them, waits for the copy to finish, then ingests them.
- **Verified ingest & renaming** — each file is moved (never copied twice)
  and renamed against the next entry of a CSV inventory, with integrity
  verification and full traceability back to the original filename.
- **Automatic framing** — OpenCV-based crop detection with a confidence
  score (reliable / needs review / impossible), plus fast manual correction.
- **Exports** — 16-bit TIFF master, JPEG master, and a JPEG reading positive
  (three rendering modes), generated in the background. The reading
  positive also automatically excludes the negative's unexposed border
  from its crop when confident enough, with a dedicated review screen for
  the cases it isn't — the archival master files always keep the full
  framed negative.
- **Metadata** — EXIF preserved, IPTC/XMP written from campaign settings,
  ICC profiles embedded.
- **Keyboard-first capture mode** — every action during capture (accept,
  reject, reframe, rename, pause) has a keyboard shortcut; the mouse stays
  optional.
- **Robustness** — resumes cleanly after a crash or forced shutdown, handles
  full disks and inaccessible folders without losing a file, resolves name
  conflicts interactively instead of overwriting anything.
- **Full audit trail** — every significant action is logged (JSON Lines),
  independent of the CSV inventory itself.

See [`USER_GUIDE.md`](USER_GUIDE.md) for the full workflow, screens, and
keyboard shortcuts.

## Requirements

- **OS**: Windows 10/11 (x64) or Linux (x64).
- **Python**: 3.11 or later.
- **exiftool** (optional but recommended): without it, exports are produced
  without embedded metadata and a warning is shown.
  - Debian/Ubuntu/Mint: `sudo apt install libimage-exiftool-perl`
  - Windows: download the executable from [exiftool.org](https://exiftool.org)
    and put it on your `PATH` (or point to it in the app's settings).
- **Hardware**: nothing special — no GPU, no heavy AI model, any machine
  from the last several years is enough. 4 GB RAM works, 8 GB is
  comfortable. **Disk space is the real constraint**, not CPU or RAM: each
  digitized negative produces a RAW file plus a 16-bit TIFF master (by far
  the largest piece — well over 100 MB at typical DSLR resolutions) and
  two JPEGs, so budget roughly 150–250 MB per image; a campaign of a few
  thousand negatives needs hundreds of gigabytes. Point the watched/output
  storage at something sized for that (external drive, NAS…) rather than a
  small internal drive. The app itself warns below 10 GB free and stops
  below 2 GB (configurable).

No network access is required or used at runtime — everything runs locally.

## Install & run

**Quick install** (downloads the app, sets up a virtual environment,
launches it):

Linux:
```sh
curl -fsSL https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.sh | bash
```
Asks where to install (default `~/ScanAssistant`). To pick the directory
without the prompt:
```sh
curl -fsSL https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.sh | bash -s -- /your/chosen/path
```

Windows (PowerShell):
```powershell
irm https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.ps1 | iex
```
Installs into `%USERPROFILE%\ScanAssistant`. To choose a different
directory, download the script first, then run it with `-TargetDir`:
```powershell
irm https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.ps1 -OutFile install.ps1
.\install.ps1 -TargetDir "D:\ScanAssistant"
```

**Manual install**, if you already have the source (clone or download):

```sh
git clone https://github.com/murypaul/ScanAssistant.git
cd ScanAssistant
./run.sh      # Windows: run.bat
```

`run.sh`/`run.bat` create a virtual environment, install the app into it,
and launch it — safe to re-run any time: the environment itself is only
created once, but the app is reinstalled into it on every launch so a
`git pull` (manual or via the in-app updater below) always takes effect.

## Updating

ScanAssistant is offline by design — no network access at runtime, with
one narrow, explicit exception for updates. Two ways to trigger it, both
opt-in, neither automatic in the background:

- **Help ▸ Check for updates…**, any time. Tells you if you're up to date
  and, if not, offers to update in place: it runs the same `git pull` the
  install scripts use, then reinstalls dependencies, right where the app
  already lives — no re-download, no new folder. Restart afterwards to use
  the new version.
- An opt-in check once at startup, off by default. Turn it on in **File ▸
  Preferences ▸ Updates**. If an update is found, a quiet note appears on
  the home screen — never a popup.

Both need `git` and a checkout with a remote configured (exactly what the
install scripts set up). If you installed from a source archive instead
(no `git` available at install time), re-run `install.sh`/`install.ps1` to
update — the in-app check will tell you this rather than fail silently.

## Why open source

This project was built primarily with AI-assisted coding tools, with a
human directing the design, the trade-offs, and the review. I think that
raises a genuine ethical question of ownership: when most of an
implementation comes from that kind of collaboration rather than purely
from hand-written effort, I don't think it's mine to keep closed — so it's
released under the GPL for anyone to use, study, or build on.

## License

GPL-3.0-or-later — see [`LICENSE`](LICENSE).

---

## Français

Un assistant de bureau pour les campagnes de numérisation. Il surveille un
dossier, ingère et renomme les fichiers RAW selon un inventaire CSV,
détecte et applique un recadrage automatiquement, génère les fichiers
dérivés, écrit les métadonnées, et journalise chaque étape — pour que
l'opérateur n'ait plus qu'à manipuler l'objet physique et vérifier le
résultat.

Conçu pour des campagnes de numérisation patrimoniale (négatifs, tirages,
autres objets plats) menées par un seul opérateur, sur des sessions longues
et interruptibles.

### Ce que fait l'application

- **Surveillance de dossier** — détecte les nouveaux fichiers RAW dès que
  l'appareil ou l'outil de transfert les écrit, attend la fin de la copie,
  puis les ingère.
- **Ingestion vérifiée et renommage** — chaque fichier est déplacé (jamais
  dupliqué) et renommé selon la prochaine entrée d'un inventaire CSV, avec
  vérification d'intégrité et traçabilité complète vers le nom d'origine.
- **Recadrage automatique** — détection du cadre par OpenCV avec un score
  de confiance (fiable / à vérifier / impossible), et correction manuelle
  rapide si besoin.
- **Exports** — TIFF maître 16 bits, JPEG maître, et un JPEG positif de
  lecture (trois modes de rendu), générés en arrière-plan. Le positif de
  lecture exclut aussi automatiquement la bordure non exposée du négatif
  de son cadrage quand la confiance est suffisante, avec un écran de
  relecture dédié pour les cas où elle ne l'est pas — les fichiers maîtres
  archivistiques gardent toujours le négatif entier.
- **Métadonnées** — EXIF conservé, IPTC/XMP écrits à partir des réglages
  de campagne, profils ICC intégrés.
- **Mode capture tout au clavier** — chaque action pendant la capture
  (valider, rejeter, recadrer, renommer, mettre en pause) a son raccourci ;
  la souris reste facultative.
- **Robustesse** — reprise propre après un arrêt brutal, disque plein ou
  dossier devenu inaccessible sans perte de fichier, conflits de noms
  résolus au cas par cas plutôt qu'un écrasement silencieux.
- **Traçabilité complète** — chaque action significative est journalisée
  (JSON Lines), indépendamment de l'inventaire CSV lui-même.

Voir [`USER_GUIDE.fr.md`](USER_GUIDE.fr.md) pour le flux complet, les
écrans, et les raccourcis clavier.

### Prérequis

- **OS** : Windows 10/11 (x64) ou Linux (x64).
- **Python** : 3.11 ou supérieur.
- **exiftool** (optionnel mais recommandé) : sans lui, les exports sont
  produits sans métadonnées embarquées, et un avertissement s'affiche.
  - Debian/Ubuntu/Mint : `sudo apt install libimage-exiftool-perl`
  - Windows : télécharger l'exécutable depuis [exiftool.org](https://exiftool.org)
    et le placer dans le `PATH` (ou l'indiquer dans les réglages de l'app).
- **Matériel** : rien de particulier — pas de GPU, pas de modèle d'IA
  lourd, n'importe quelle machine des dernières années suffit. 4 Go de RAM
  fonctionnent, 8 Go sont confortables. **L'espace disque est la vraie
  contrainte**, pas le CPU ni la RAM : chaque négatif numérisé produit un
  RAW, un TIFF maître 16 bits (de loin le plus gros — largement plus de
  100 Mo aux résolutions habituelles de reflex) et deux JPEG, donc comptez
  environ 150 à 250 Mo par image ; une campagne de quelques milliers de
  négatifs demande plusieurs centaines de gigaoctets. Prévoyez un stockage
  (disque externe, NAS…) dimensionné en conséquence plutôt qu'un petit
  disque interne. L'app elle-même avertit sous 10 Go libres et s'arrête
  sous 2 Go (seuils configurables).

Aucun accès réseau n'est requis ni utilisé à l'exécution — tout tourne en
local.

### Installation et lancement

**Installation rapide** (télécharge l'app, prépare un environnement
virtuel, la lance) :

Linux :
```sh
curl -fsSL https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.sh | bash
```
Demande où installer (par défaut `~/ScanAssistant`). Pour choisir le
dossier sans passer par l'invite :
```sh
curl -fsSL https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.sh | bash -s -- /chemin/de/votre/choix
```

Windows (PowerShell) :
```powershell
irm https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.ps1 | iex
```
Installe dans `%USERPROFILE%\ScanAssistant`. Pour choisir un autre
dossier, téléchargez d'abord le script puis lancez-le avec `-TargetDir` :
```powershell
irm https://raw.githubusercontent.com/murypaul/ScanAssistant/master/install.ps1 -OutFile install.ps1
.\install.ps1 -TargetDir "D:\ScanAssistant"
```

**Installation manuelle**, si vous avez déjà les sources (clone ou
téléchargement) :

```sh
git clone https://github.com/murypaul/ScanAssistant.git
cd ScanAssistant
./run.sh      # Windows : run.bat
```

`run.sh`/`run.bat` créent un environnement virtuel, y installent l'app, et
la lancent — sans risque à relancer : l'environnement lui-même n'est créé
qu'une fois, mais l'app y est réinstallée à chaque lancement, pour qu'un
`git pull` (manuel ou via la mise à jour intégrée ci-dessous) soit toujours
pris en compte.

### Mise à jour

ScanAssistant est hors ligne par conception — aucun accès réseau à
l'exécution, avec une seule exception explicite et ciblée pour les mises à
jour. Deux façons de la déclencher, toutes deux volontaires, jamais
automatiques en tâche de fond :

- **Help ▸ Check for updates…**, à tout moment. Indique si vous êtes à
  jour et, sinon, propose de mettre à jour sur place : la même commande
  `git pull` que les scripts d'installation, puis une réinstallation des
  dépendances, exactement là où l'app se trouve déjà — pas de nouveau
  téléchargement, pas de nouveau dossier. Redémarrez ensuite pour utiliser
  la nouvelle version.
- Une vérification automatique unique au démarrage, désactivée par défaut.
  À activer dans **File ▸ Preferences ▸ Updates**. Si une mise à jour est
  trouvée, une note discrète apparaît sur l'écran d'accueil — jamais de
  popup.

Les deux nécessitent `git` et une copie du dépôt avec un remote configuré
(exactement ce que mettent en place les scripts d'installation). Si vous
avez installé depuis une archive source (pas de `git` disponible à
l'installation), relancez `install.sh`/`install.ps1` pour mettre à jour —
la vérification intégrée vous l'indiquera plutôt que d'échouer en silence.

### Pourquoi open source

Ce projet a été construit principalement avec des outils de développement
assistés par IA, un humain dirigeant la conception, les arbitrages et la
relecture. Cela pose selon moi une véritable question éthique de
propriété : quand l'essentiel d'une implémentation vient de ce type de
collaboration plutôt que d'un travail manuel classique, je ne considère pas
qu'elle m'appartienne en propre — d'où la publication sous licence GPL,
ouverte à qui veut l'utiliser, l'étudier ou la reprendre.

### Licence

GPL-3.0-or-later — voir [`LICENSE`](LICENSE).
