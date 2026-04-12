# Robot de trading pour <a href="https://polymarket.com/">Polymarket</a>.   

# Généralités
Stratégie de codage : ré-utiliser les code des meilleurs robots de trading existant.  
Architecture technique : utilisation de container Docker.   
Accès à l'application : <a href="https://localhost:8888">http://localhost:8888</a>.   
Stratégie d'investissement : copier les meileurs portefeuilles.  

# Interface utilisateur

## Onglet "Top Traders"
Cet écran permet de visualiser les meilleurs traders de Polymarket.   
Liste des meilleurs traders de Polymarket
- Nom
- Adresse
- PNL
- Volume
- Bouton "Suivre"

## Onglet "Analysis"
Cet écran permet de visualiser les décisions prise par le bot.   
Liste des paris intéressants
- Trader
- Nom du pari
- Pari

- ## Onglet "Dashboard"
Cet écran permet de visualiser les paris pris par le bot.   
Liste des paris pris
- Nom du pari
- Pari
- Montant du pari
- Status (à prendre, en cours, terminé)
- Résulat (en valeur absolue)
- Résulat (en pourcentage)

## Onglet "Settings"
Champs de connection à Polymarket via l'API Key.   
Bouton de démarrage du bot.    
