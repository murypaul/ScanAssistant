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
avec surimpression) ; `P` bascule vers l'aperçu positif.

## Raccourcis clavier

Tout ce qui suit se fait sans toucher la souris, qui reste disponible à
tout moment. Les raccourcis reposent sur des lettres (indépendants de la
disposition du clavier). Ce sont les valeurs par défaut — modifiez-les
depuis **File ▸ Preferences ▸ Shortcuts** (cliquez sur la touche actuelle,
puis appuyez sur la nouvelle). Les déplacements et la rotation du cadre
(flèches, +/−, Ctrl+flèches) restent fixes — c'est un geste spatial, pas un
raccourci à choisir — de même que les rôles de Tab et Échap dans le
panneau de conflit de nom.

### Capture

| Touche | Action |
| ------ | ------ |
| Entrée | Valider l'image courante (équivaut à l'arrivée de la suivante) |
| R | Rejeter l'image courante |
| V | Rotation 90° (cycle 0°→90°→180°→270°) — Maj+V tourne dans l'autre sens |
| Ctrl+G | Aller à un nom déjà en attente (autocomplétion à la saisie) |
| C | Recalculer le cadre (relance la détection automatique) |
| P | Basculer l'aperçu positif |
| T | Basculer l'aperçu maître (cadre appliqué) |
| K | Cycler l'aperçu (négatif → positif → maître), indépendant de P/T — Maj+K cycle dans l'autre sens |
| Espace | Pause / reprendre |
| F11 | Plein écran |
| Échap | Arrêter la capture (retour à la préparation ; les exports continuent) |

### Ajuster le cadre

Aucun mode à activer — toujours disponible sur l'aperçu négatif brut
(y ramène automatiquement si un aperçu positif/maître était affiché) :

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

### Panneau de conflit de nom

| Touche | Action |
| ------ | ------ |
| 1 / 2 / 3 | Choisir l'option correspondante |
| Tab / Maj+Tab | Naviguer entre options et champs |
| Entrée | Valider l'option sélectionnée |
| Échap | Équivaut à l'option 1 avec un champ vide |

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

## Aperçu positif

Trois modes de rendu pour le JPEG positif de lecture, réglés par
campagne :

- **simple** — normalisation min/max linéaire, rien d'autre.
- **auto** (par défaut) — une optimisation exposition/gamma déterministe,
  sans apprentissage automatique : le résultat est identique à chaque
  fois pour une même entrée.
- **manual** — réglages de campagne (exposition, ombres, hautes lumières,
  contraste), ajustables à chaud pendant la capture avec aperçu (`P`).

Le positif de lecture exclut aussi automatiquement la bordure non exposée
du négatif de son cadrage, dès qu'il peut distinguer les deux avec
confiance — le TIFF et le JPEG maîtres gardent toujours le négatif entier,
bordure comprise, pour la fidélité archivistique. L'exposition automatique
n'est plus faussée par cette bordure, que ce rognage supplémentaire
réussisse ou non.

Quand la confiance n'est pas suffisante pour tracer ce rognage
supplémentaire tout seul, l'image reste simplement le négatif entier
recadré au cadre support — jamais de coupe décidée sur une estimation peu
fiable. **Project ▸ Positive crop review** (aussi accessible en dehors de
la capture) liste chaque image laissée ainsi, affiche l'image déjà
exportée avec un rectangle de recadrage déplaçable à la souris, et permet
de confirmer ou d'ajuster le cadrage et l'exposition pour cette image
précise — `Enter` confirme et passe à la suivante ; seul le positif de
lecture est régénéré.

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
  si la fermeture attend les exports en cours, et la longueur maximale
  d'un nom de négatif acceptée à l'import d'un nouveau CSV.
- **Thresholds** — les seuils d'espace disque (avertissement/critique) et
  le seuil d'alerte précoce de la file d'export.
- **Updates** — la vérification automatique au démarrage, volontaire (voir
  [Mise à jour](README.md#mise-à-jour) dans le README), et un bouton de
  vérification manuelle.
- **Shortcuts** — tous les raccourcis reconfigurables (voir plus haut),
  avec réinitialisation individuelle ou globale.

**Export settings…**/**Import settings…**, en bas de cette fenêtre,
sauvegardent ou rechargent l'ensemble du fichier (raccourcis reconfigurés
compris) en JSON — pratique pour reporter ses réglages sur une autre
machine ou garder une sauvegarde avant d'expérimenter. Tout ceci vit aussi
dans un simple fichier JSON géré par l'OS (`platformdirs` — par exemple
`~/.config/scanassistant/config.json` sous Linux), pour qui préfère
l'éditer directement.
