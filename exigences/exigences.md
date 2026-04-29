# Robot de trading pour [Polymarket](https://polymarket.com/)

# Généralités

**Stratégie de codage :** ré-utiliser le code des meilleurs robots de trading existant.
**Architecture technique :** utilisation de container Docker.
**Accès à l'application :** [http://localhost:8888](http://localhost:8888).

# Stratégie d'investissement : Trading des marchés météo

Le bot exploite une **inefficience informationnelle** sur les marchés météo de Polymarket :

1. **Détection** : le bot identifie les marchés météo actifs sur Polymarket (ex: "Will it rain in London tomorrow?", "Temperature > 20°C in NYC?").
2. **Comparaison** : il récupère la **prévision météo officielle** depuis une API publique (Open-Meteo, OpenWeatherMap, Met Office, NOAA) pour la ville et la date concernée.
3. **Décision** :
   - Si la prévision officielle dit "OUI à 80%" mais le marché Polymarket cote à 60% → le bot **achète YES** (sous-évalué)
   - Si la prévision officielle dit "NON à 80%" mais le marché cote à 30% pour NON → le bot **achète NO** (sous-évalué)
4. **Edge** : les prévisions météo officielles sont fiables à >90% pour les prévisions à 24h. Le bot exploite l'écart entre cette fiabilité et le prix de marché.

## Sources de prévisions

- [Open-Meteo](https://open-meteo.com/) (gratuit, sans clé API) — source primaire
- [OpenWeatherMap](https://openweathermap.org/) (clé API gratuite) — source de secours

## Villes ciblées

Par défaut : Londres, New York, Paris, Tokyo, Seoul (les marchés les plus liquides).
Configurable via l'IHM.

# Interface utilisateur

## Onglet "Markets"

Liste des marchés météo actifs détectés sur Polymarket.

Colonnes :
- Nom du marché
- Ville
- Date de résolution
- Prix actuel YES / NO
- Prévision officielle (probabilité)
- Edge (différence prix / prévision)
- Bouton "Parier" (si non déjà pris)

## Onglet "Analysis"

Décisions prises par le bot avec justification.

Colonnes :
- Marché
- Prix marché
- Prévision officielle (% + source)
- Edge calculé
- Décision (YES, NO, Skip)
- Raison du skip si applicable

## Onglet "Dashboard"

Paris pris par le bot avec résultats.

Colonnes :
- Nom du pari
- Pari (YES / NO)
- Montant investi
- Statut (À prendre, En cours, Terminé)
- Résultat (en valeur absolue)
- Résultat (en pourcentage)

Cartes en haut :
- Portefeuille total
- Disponible (USDC)
- En positions
- PnL total

## Onglet "Settings"

- Champs de connexion à Polymarket (clé privée Magic Link)
- Bouton de démarrage / arrêt du bot
- Configuration des villes à surveiller
- Paramètres de risque :
  - Edge minimum requis pour parier (ex: 10%)
  - Montant min / max par pari
  - Exposition totale max
- Configuration des sources météo (clé OpenWeatherMap optionnelle)
