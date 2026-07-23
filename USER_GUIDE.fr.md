# Guide utilisateur

*[English version available here](USER_GUIDE.md).*

Ce guide couvre le flux complet : créer une campagne, mener une session de
capture, et tout ce que propose l'interface. Pour l'installation, voir
[`README.md`](README.md#français).

## Vue d'ensemble

Une **campagne**, c'est une session de numérisation : une arborescence de
dossiers, un fichier de réglages, et un inventaire (une liste CSV de noms à
attribuer, dans l'ordre). Une fois la campagne ouverte, le **mode capture**
fait le reste :

1. Vous posez un négatif sur la table lumineuse et déclenchez. L'appareil
   (ou son outil de transfert) écrit le fichier RAW dans le dossier
   surveillé.
2. L'application détecte le nouveau fichier, attend que sa taille se
   stabilise (signe que la copie est terminée), puis le déplace et le
   renomme selon la prochaine entrée de l'inventaire.
3. Un aperçu s'affiche avec le cadre détecté et un indicateur de confiance.
4. Les exports TIFF, JPEG maître et JPEG positif se génèrent en
   arrière-plan, métadonnées incluses.
5. Vous posez l'objet suivant. Le précédent est automatiquement validé dès
   l'arrivée du suivant — rejetez-le d'abord (`R`) si quelque chose
   clochait.

Rien n'est jamais supprimé : un RAW rejeté part dans `REJECTED/`, un
fichier remplacé part dans `BACKUP/`, et le dossier surveillé n'est vidé
que des fichiers déjà ingérés en toute sécurité.

## Créer une campagne

Depuis l'écran d'accueil, **New campaign** ouvre un assistant en plusieurs
étapes :

1. **Identité** — nom, description, opérateur, institution, support.
2. **Dossiers** — emplacement de la campagne, et quel dossier surveiller
   pour les fichiers entrants (les fichiers qui y sont déposés sont
   déplacés dans la campagne une fois vérifiés).
3. **CSV** — choisir le fichier d'inventaire ; l'application détecte son
   format et vous demande de confirmer quelle colonne contient les noms,
   avec un aperçu et un rapport de validation avant l'import.
4. **Prise de vue et recadrage** — orientation par défaut, mode de
   dimensions de sortie, marges, recadrage automatique activé ou non.
5. **Exports** — réglages pour le TIFF, le JPEG maître et le JPEG positif.
6. **Métadonnées** — champs IPTC écrits sur chaque export (créateur,
   institution, copyright, collection, mots-clés).
7. **Récapitulatif** — vérification, puis création. Tout reste modifiable
   ensuite depuis l'écran projet.

## L'écran projet

Il s'ouvre en dehors du mode capture. Un bouton **Start capture** reste
visible en permanence sous les onglets — plus rapide à atteindre que
le menu ou le raccourci clavier. Onglets : **Summary**, **Folders**,
**Capture**, **Framing**, **Exports**, **Metadata**, **CSV** (table de
l'inventaire en lecture seule, avec recherche, filtre par statut et
positionnement du curseur sur une ligne), et **Log** (événements du jour,
filtrables, avec un raccourci vers le dossier de logs).

Chaque changement de réglage est appliqué et enregistré immédiatement — il
n'y a pas d'étape de sauvegarde séparée.

L'onglet **Summary** propose un bouton **Reset campaign…** (confirmation
obligatoire) pour repartir de zéro : chaque ligne repasse à todo, le
curseur revient au début, et les exports TIFF/JPEG déjà produits sont
supprimés (toujours régénérables depuis les RAW). Les négatifs capturés ne
sont jamais supprimés — ils sont déplacés dans un dossier horodaté sous
`BACKUP/`, de sorte que le dossier RAW redevienne vide et prêt pour de
nouvelles prises sous les mêmes noms, sans jamais rien perdre. Les
paramètres de la campagne eux-mêmes ne sont pas touchés.

## Mode capture

Utilisable en plein écran, une image à la fois :

```
┌──────────────────────────────────────────────────────────────────┐
│ File  Project  Capture  Processing  Metadata  View  Help          │
├──────────────────────────────────────────────────────────────────┤
│  NEG_00125            ● RELIABLE 0.94                              │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│                  [ aperçu de l'image courante ]                   │
│              cadre détecté en surimpression, coloré               │
│                                                                    │
├──────────────────────────────────────────────────────────────────┤
│  next: NEG_00126   export queue: 2   127/842 · 15%                │
│  TIFF written (NEG_00124)                            ● CAPTURE    │
└──────────────────────────────────────────────────────────────────┘
```

Rien n'est jamais dessiné par-dessus l'image elle-même à part le cadre
détecté — nom, confiance et progression vivent dans des bandeaux
au-dessus et en dessous de l'aperçu. Le cadre superposé est vert
(fiable), orange (à vérifier), ou rouge (indéterminé — repli sur l'image
entière) ; il porte un liseré sombre des deux côtés de sa couleur pour
rester visible quelle que soit la teinte propre du négatif. `T` bascule
vers l'aperçu du cadre réellement appliqué (plutôt que le cadre brut
avec surimpression).

Un petit histogramme de luminance, translucide, se trouve dans un coin de
l'aperçu — un repère rapide sur l'exposition. Il ne suit que la vue
négatif, pas le basculement vers l'aperçu maître ci-dessus. Il n'y a pas
d'aperçu positif pendant la capture : le jugement de tonalité se fait
entièrement dans l'écran dédié [Calibrage positif](#calibrage-positif),
après la capture — voir plus bas. La capture elle-même se juge sur le
négatif brut et son histogramme.

## Raccourcis clavier

Tout ce qui suit se fait sans toucher la souris, qui reste disponible à
tout moment. Les raccourcis reposent sur des lettres (indépendants de la
disposition du clavier). Ce sont les valeurs par défaut — modifiez-les
depuis **File ▸ Preferences ▸ Shortcuts** (cliquez sur la touche actuelle,
puis appuyez sur la nouvelle). Les déplacements et la rotation du cadre
(flèches, +/−, Ctrl+flèches) restent fixes — c'est un geste spatial, pas un
raccourci à choisir — de même que le rôle d'Échap comme annulation dans le
panneau de conflit de nom. Tab reste reconfigurable en Capture (ci-dessous)
mais garde toujours son rôle habituel de navigation entre champs/options
dans le panneau de conflit de nom, puisqu'un seul des deux contextes est
actif à la fois.

### Capture

| Touche | Action |
| ------ | ------ |
| Entrée | Valider l'image courante (équivaut à l'arrivée de la suivante) |
| R | Rejeter l'image courante |
| V | Rotation 90° (cycle 0°→90°→180°→270°) — Maj+V tourne dans l'autre sens |
| Ctrl+G | Aller à un nom déjà en attente (autocomplétion à la saisie) |
| C | Recalculer le cadre (relance la détection automatique) |
| T | Basculer l'aperçu maître (cadre appliqué) |
| K | Cycler l'aperçu (négatif → maître), même bascule que T — Maj+K cycle dans l'autre sens |
| Tab | Pause / reprendre |
| Espace | Déclencher une capture à distance (appareil tethered uniquement, voir [Capture tethered](#capture-tethered-live-view-et-déclenchement-à-distance) ci-dessous) |
| L | Activer/désactiver le live view (appareil tethered uniquement) |
| W | Choisir la balance des blancs sur un point neutre de l'aperçu (cliquer après avoir appuyé sur W) — s'applique au reste de la séance, aperçu et exports ; Échap annule |
| F11 | Plein écran |
| Échap | Arrêter la capture (retour à la préparation ; les exports continuent) |

### Ajuster le cadre

Aucun mode à activer — toujours disponible sur l'aperçu négatif brut
(y ramène automatiquement si l'aperçu maître était affiché) :

| Entrée | Action |
| ------ | ------ |
| Flèches | Déplacer le cadre d'1 pixel d'aperçu (Maj : ×10) |
| + / − | Agrandir / réduire de 1 % (Maj : 5 %), centre conservé |
| Ctrl+← / Ctrl+→ | Rotation ∓0,1° (Maj : ×10), bornée à ±45° |
| G | Afficher/masquer les guides règle des tiers |
| Glisser un bord ou un coin (souris) | Redimensionner depuis ce côté/coin |
| Glisser l'intérieur (souris) | Déplacer tout le cadre |

Chaque modification se stabilise en un seul export après une courte pause
(ou immédiatement, au relâchement de la souris) — rien à valider ni à
annuler. En cas d'erreur, glissez le cadre en arrière ou appuyez sur `C`
pour revenir à la détection automatique.

### Balance des blancs

La balance des blancs automatique du boîtier dérive d'une prise à l'autre
selon ce qui se trouve dans le cadre — mais la table lumineuse en dessous
reste la même pour toute la séance, donc une balance fixée une fois pour
toutes donne un rendu plus cohérent qu'une estimation refaite à chaque
négatif.

Appuyez sur `W` sur la première capture de la séance (une vue de la table
à vide convient bien) — le curseur devient une croix — puis cliquez sur un
point neutre de l'aperçu. Cette balance s'applique ensuite à la fois à
l'aperçu à l'écran et à chaque export, pour toute la durée où la campagne
reste ouverte (un arrêt/reprise de la capture ne la réinitialise pas).
`W` à nouveau ou Échap annule la sélection sans rien changer.

### Panneau de conflit de nom

| Touche | Action |
| ------ | ------ |
| 1 / 2 / 3 | Choisir l'option correspondante |
| Tab / Maj+Tab | Naviguer entre options et champs |
| Entrée | Valider l'option sélectionnée |
| Échap | Équivaut à l'option 1 avec un champ vide |

### Écran de calibrage positif

Ctrl+clic/Maj+clic (souris) sélectionne aussi plusieurs vignettes dans
la grille.

| Touche | Action |
| ------ | ------ |
| Entrée | Confirmer et suivant — applique les réglages de l'image courante, avance dans le filtre actif |
| Haut / Bas | Image précédente / suivante dans la liste filtrée |
| Page préc. / Page suiv. | Saut plus large dans la même liste |
| Ctrl+A | Sélectionner toutes les images du filtre actif |
| Ctrl+Entrée | Appliquer à la sélection — copie les réglages de tonalité de l'image courante vers la sélection (Dmin exclu par défaut) |
| Ctrl+Z / Ctrl+Y | Annuler / rétablir le dernier réglage confirmé ou la dernière propagation |
| Échap | Fermer l'écran, retour au projet |

### Partout ailleurs

| Touche | Action |
| ------ | ------ |
| Ctrl+N | Nouvelle campagne |
| Ctrl+O | Ouvrir une campagne |
| Ctrl+Q | Quitter |
| F5 | Démarrer la capture |
| Ctrl+F | Rechercher dans le visualiseur CSV |
| F1 | Cette liste de raccourcis |
| F11 | Plein écran |
| Alt+lettre | Ouvrir le menu correspondant |

## Capture tethered (live view et déclenchement à distance)

Optionnel, désactivé par défaut (à activer dans **File ▸ Preferences ▸
Camera** — prend effet au prochain démarrage de l'application). Le
firmware Nikon éteint l'écran arrière de l'appareil tant qu'il est
connecté en USB : impossible de voir ce qu'on cadre sur l'appareil
lui-même — cette fonctionnalité affiche le flux en direct à l'écran à la
place, et permet de déclencher au clavier. Elle ne remplace pas le
chargement du film et le cadrage à la main, et ne pilote à distance ni
l'exposition, ni l'ISO, ni l'ouverture, ni la mise au point — seuls le
flux en direct et le déclenchement sont concernés.

Le **Nikon D750** est le premier boîtier pris en charge (USB/PTP). Avant
de connecter l'appareil :

- Sur l'appareil, régler **menu Setup ▸ USB** sur **PTP/MTP** (pas
  *Mass Storage*) — sinon l'OS le monte comme un simple disque USB au
  lieu d'un appareil photo.
- Sur Linux Mint (et les autres bureaux Cinnamon/GNOME), le service
  `gvfs`/`gvfsd-gphoto2` de l'OS tente de capter l'appareil en premier et
  peut bloquer l'application avec un message « connexion USB déjà
  utilisée » — le fermer (ou désactiver le démarrage automatique de
  `gvfsd-gphoto2` et `gvfs-gphoto2-volume-monitor`) avant d'ouvrir le
  mode capture.
- L'appareil ne tenant qu'une seule connexion USB à la fois, le dossier
  surveillé de la campagne ne peut pas être le point de montage de la
  carte mémoire tant que la capture tethered est active — pointez-le
  plutôt vers un dossier local ordinaire (le fichier déclenché y est
  téléchargé automatiquement).

Une fois activé et l'appareil connecté :

- **`L`** active/désactive la vignette de live view, dans un coin de la
  zone d'aperçu. Elle ne démarre jamais toute seule — l'activer maintient
  le miroir de l'appareil relevé, pensez à la désactiver une fois le
  cadrage vérifié.
- **`Espace`** déclenche l'obturateur à distance et télécharge le RAW
  obtenu directement dans le dossier surveillé, sur cette même
  connexion — rien à brancher, débrancher ou remonter entre deux prises.
  Il suit ensuite exactement le même chemin qu'un fichier transféré à la
  main : nommage, aperçu, exports fonctionnent de la même façon. Le
  négatif reste par ailleurs sur la carte mémoire, donc une connexion
  coupée ou un téléchargement en échec ne fait perdre aucune prise.
- Faites glisser le curseur d'opacité de la vignette pour voir au travers
  le dernier aperçu accepté ; cliquez sur la vignette pour l'agrandir et
  utilisez la molette/le glissement de la souris pour zoomer et vous
  déplacer et vérifier la mise au point de près, cliquez à nouveau (ou
  sur l'icône de réduction) pour revenir à la petite vignette.
- Faites glisser la vignette repliée elle-même pour la repositionner où
  vous voulez sur l'aperçu (un simple clic l'agrandit toujours) — elle
  reste à sa place d'une session à l'autre.
- Le réglage de fréquence est un plafond, pas une garantie : le live view
  en USB 2.0 plafonne en général autour de 10-20 fps quel que soit le
  réglage choisi ; la vignette affiche la fréquence réellement atteinte à
  côté du réglage.

## Recadrage et confiance

Le cadre est détecté automatiquement sur l'aperçu embarqué, par vision
classique, sans présumer d'un ratio ou d'une taille — ce qui permet de
traiter des formats mélangés. Chaque détection reçoit un score de
confiance basé sur cinq critères indépendants : le taux de remplissage du
cadre, sa rectangularité, la plausibilité de sa taille, le contact avec le
bord de l'image, et sa solidité.

- **Fiable** (vert) — utilisé tel quel.
- **À vérifier** (orange) — mérite un coup d'œil avant de passer à la
  suite.
- **Impossible** (rouge) — un négatif très sous-exposé (contraste quasi
  nul avec la table lumineuse) reçoit d'abord automatiquement une seconde
  tentative plus poussée ; si elle échoue aussi, repli sur l'image entière
  non recadrée — à corriger manuellement si besoin (voir *Ajuster le
  cadre* ci-dessus).

Les corrections manuelles et les nouvelles détections ne régénèrent les
exports que de l'image concernée ; les images déjà finalisées restent
inchangées (pour celles-ci, passez plutôt par la vérification de
complétude et sa régénération).

## Calibrage positif

Pas d'aperçu positif pendant la capture — le jugement de tonalité se fait
entièrement après coup, dans **Project ▸ Positive calibration** (aussi
accessible en dehors de la capture), un écran dédié qui prend toute la
fenêtre principale, comme le mode capture plutôt qu'une fenêtre flottante
à part. La capture elle-même se juge sur le négatif brut et son
histogramme.

Deux moteurs de positif interchangeables, réglés par campagne :

- **Historique** — le pipeline par courbe de tonalité d'origine, trois
  modes de rendu (**simple** : normalisation min/max linéaire, rien
  d'autre ; **auto**, par défaut : une optimisation exposition/gamma
  déterministe, sans apprentissage automatique, résultat identique à
  chaque fois pour une même entrée ; **manual** : réglages de campagne
  exposition/ombres/hautes lumières/contraste).
- **Domaine de densité** (`print_engine`) — reconstruit le procédé de
  tirage argentique lui-même plutôt que d'étirer une courbe déjà
  inversée : une base du film par canal (Dmin, échantillonnée sur la
  bordure non exposée du négatif lui-même — c'est ce qui absorbe un
  support jauni ou taché dans un résultat neutre), une courbe de réponse
  du film fixe, et une réponse papier (exposition, contraste, point noir,
  compression douce des hautes lumières). Plus fidèle au fonctionnement
  physique, à un coût réel : un décodage RAW dédié et un rendu bien plus
  lourd (de quelques secondes à plusieurs dizaines de secondes par image
  selon la machine) — pendant la capture, il tourne sur un bassin
  d'ouvriers séparé, dimensionné par **Préférences ▸ Traitement ▸
  Ouvriers de finalisation du positif**, pour ne jamais ralentir l'export
  TIFF/JPEG maître.

Le positif de lecture exclut aussi automatiquement la bordure non exposée
du négatif de son cadrage, dès qu'il peut distinguer les deux avec
confiance — le TIFF et le JPEG maîtres gardent toujours le négatif entier,
bordure comprise, pour la fidélité archivistique. L'exposition automatique
n'est plus faussée par cette bordure, que ce rognage supplémentaire
réussisse ou non. Une image dont le moteur n'est pas confiant — cadrage,
ou pour le moteur en domaine de densité, tonalité — reste signalée pour
vérification plutôt qu'acceptée en silence.

### L'écran de calibrage

Une grille de vignettes (réutilisant les JPEG déjà exportés, sans nouveau
décodage) à gauche, filtrable par catégorie à cocher — à vérifier par
défaut, mais aussi déjà appliquées avec confiance ou déjà confirmées
manuellement, si vous voulez les revérifier — avec sélection multiple
(clic, Ctrl/Maj+clic, ou `Ctrl+A` pour tout le filtre courant). Le
panneau de droite dépend du moteur de la campagne :

- **Moteur historique** : les mêmes réglages exposition/ombres/hautes
  lumières/contraste que le mode manuel en capture, plus un rectangle de
  recadrage déplaçable sur l'aperçu, mis à jour en direct.
- **Moteur en domaine de densité** : quatre groupes — base du film
  (Dmin), exposition du scan, modèle du film (fixe, affiché pour
  information, non réglable — c'est une propriété de la pellicule, pas
  un choix par image), et modèle du papier (contraste, avec point
  noir/compression douce sous un bouton **Avancé** dont l'état ouvert/
  fermé persiste d'une image à l'autre). Chaque groupe démarre en
  **Auto** ; son propre interrupteur bascule en **Manual** sans toucher
  aux autres. Pas de rectangle de recadrage ici pour l'instant — le
  moteur en domaine de densité n'a pas encore de surcharge de cadrage
  manuel, seulement les groupes de tonalité. Le rendu étant coûteux,
  l'aperçu ne se met à jour qu'une fois un réglage validé (curseur
  relâché, Entrée dans un champ), jamais pendant le glissement — la
  fenêtre affiche un curseur d'attente pour ce rendu plutôt que de geler
  en silence.

`Enter` (ou **Confirm & next**) applique les réglages de l'image courante
et passe à la suivante du filtre actif. Pour le moteur en domaine de
densité, **Apply to selection** (bouton, ou `Ctrl+Enter`) copie les
réglages de tonalité de l'image courante vers toutes les autres images
sélectionnées d'un coup — utile pour toute une pellicule prise dans les
mêmes conditions. Le Dmin est exclu de cette propagation par défaut
(cochez **Include Dmin** pour l'inclure) : c'est une mesure physique
propre à la bordure de ce négatif précis, pas un choix esthétique à
copier sans discernement sur toute une sélection. Une confirmation
indique combien d'images sont concernées avant toute régénération.

`Ctrl+Z` / `Ctrl+Y` annulent et rétablissent le dernier réglage confirmé
ou la dernière propagation — jamais un glissement en cours — pour la
session d'écran courante (la pile n'est pas conservée en quittant
l'écran). `Échap` revient à l'écran projet. Un choix confirmé reste
valable même si l'image est retraitée plus tard (après une coupure, une
nouvelle tentative).

Le TIFF et le JPEG maîtres ne sont jamais affectés par tout cela — seul le
positif de lecture change.

## Conflits de noms

Si le nom que l'application s'apprête à attribuer existe déjà sur disque,
la capture se met en pause sur ce fichier (les prises suivantes
s'accumulent en attente) et un panneau apparaît :

- **Rename current image** — donner un autre nom au fichier entrant
  (`<NAME>_BIS` par défaut) ; la ligne d'inventaire d'origine reste en
  attente.
- **Replace existing** — déplacer tous les fichiers existants sous ce nom
  (RAW et exports) vers `BACKUP/`, puis ingérer le nouveau sous le nom
  d'origine.
- **Rename existing file** — renommer les fichiers existants à la place
  (`<NAME>_OLD` par défaut) et ingérer le nouveau sous le nom d'origine.

Rien n'est jamais écrasé silencieusement.

## Alertes, erreurs et reprise

Trois niveaux, jamais bloquants :

- **Info** — ligne de statut, disparaît après 5 secondes. Événements
  courants uniquement ; rien qui ne demande d'action de votre part.
- **Avertissement** — bandeau au-dessus de la ligne de statut, cliquable
  pour le détail ou fermable par le ×. N'interrompt rien (par exemple :
  outil de métadonnées introuvable, fichier résiduel impossible à
  nettoyer, file d'export qui grossit).
- **Critique** — bandeau rouge, le pipeline se met en pause (par exemple :
  disque presque plein, dossier devenu inaccessible). Les détections
  continuent de s'accumuler ; rien n'est perdu. Une fois la cause résolue,
  cliquez sur **Resume processing**.

Si l'application a été fermée brutalement (crash, coupure), la réouverture
de la campagne affiche un court rapport de reprise : l'image en cours est
finalisée, les fichiers temporaires orphelins sont nettoyés, et tout export
inachevé est remis en file — automatiquement, rien à refaire à la main.

Une fermeture normale pendant que des exports sont encore en cours affiche
un petit panneau « Finalizing: N export(s) pending » plutôt que de geler la
fenêtre — patientez, ou cliquez sur **Quit without waiting** pour fermer
immédiatement (ces exports reprennent automatiquement à la prochaine
ouverture de la campagne, comme après un crash). Ce comportement se règle
dans File ▸ Preferences ▸ Processing.

## Statistiques et complétude

**Project ▸ Statistics** (aussi disponible hors capture, dès qu'une
campagne a des entrées) affiche les totaux, le nombre d'images faites, en
attente, rejetées ou en erreur, et une **vérification de complétude** :
pour chaque ligne marquée faite, elle confirme que le RAW renommé et
chaque export attendu existent bien sur disque, liste ce qui manque, et
peut régénérer la sélection en une action.

## Configuration

Les réglages de campagne sont stockés dans `campaign.json`, à l'intérieur
du dossier de campagne, et se modifient depuis l'écran projet — aucune
édition manuelle n'est nécessaire.

Les préférences propres à la machine se règlent depuis **File ▸
Preferences** (désactivé pendant la capture, comme la plupart des menus),
appliquées et enregistrées dès le changement :

- **General** — rouvrir la dernière campagne au démarrage.
- **Processing** — le chemin d'exiftool (avec un bouton parcourir/tester),
  si la fermeture attend les exports en cours, la longueur maximale d'un
  nom de négatif acceptée à l'import d'un nouveau CSV, et le nombre
  d'ouvriers dédiés au moteur positif en domaine de densité pendant la
  capture (voir [Calibrage positif](#calibrage-positif)).
- **Thresholds** — les seuils d'espace disque (avertissement/critique) et
  le seuil d'alerte précoce de la file d'export.
- **Updates** — la vérification automatique au démarrage, volontaire (voir
  [Mise à jour](README.md#mise-à-jour) dans le README), et un bouton de
  vérification manuelle.
- **Camera** — active la capture tethered (voir [Capture
  tethered](#capture-tethered-live-view-et-déclenchement-à-distance)
  ci-dessus). Désactivé par défaut ; prend effet au prochain démarrage.
- **Shortcuts** — tous les raccourcis reconfigurables (voir plus haut),
  avec réinitialisation individuelle ou globale.

**Export settings…**/**Import settings…**, en bas de cette fenêtre,
sauvegardent ou rechargent l'ensemble du fichier (raccourcis reconfigurés
compris) en JSON — pratique pour reporter ses réglages sur une autre
machine ou garder une sauvegarde avant d'expérimenter. Tout ceci vit aussi
dans un simple fichier JSON géré par l'OS (`platformdirs` — par exemple
`~/.config/scanassistant/config.json` sous Linux), pour qui préfère
l'éditer directement.
