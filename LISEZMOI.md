# Bot serveur Sleeo Watch (24h/24)
1. **Diagnostic d'abord** : `python diagnostic.py` sur le serveur (Render > Shell). Il dit si Vinted répond et si les annonces sont lisibles. Je n'ai pas pu tester depuis mon environnement.
2. Render : service **Background Worker** (ou Web Service avec keep_alive), commande de démarrage `python bot.py`, `pip install -r requirements.txt`.
3. Variables d'environnement : voir `.env.example` (la clé `SUPABASE_SERVICE_KEY` est SECRÈTE, jamais dans le site).
4. Discord > Portail développeur > Bot : activer **Server Members Intent** et **Message Content Intent**. Dans ton serveur, crée les rôles Start, Plus, Max pour la synchronisation, et mets l'identifiant du serveur dans GUILD_ID.
5. Tape `!setup` (administrateur) pour le panneau de filtres, `!status` pour l'état.
## Niches intégrées
Dans le site (admin) ou dans Supabase > table `niches` : nom, lien de recherche Vinted, ID du salon Discord, niveau (basic/adv). Le bot les surveille toutes les 20 s pour tout le monde et poste dans le salon.
## Réalité du 24h/24
Vinted bloque souvent les serveurs d'hébergement : sans proxys résidentiels, attends-toi à des 403. Chaque lecture d'une page de recherche pèse environ 100 à 500 Ko : à 20 s d'intervalle, une seule source fait plusieurs Go par jour. Prévois un forfait proxys, et regroupe les niches (les mêmes liens sont lus une seule fois pour tous). Le scraping de Vinted est contraire à ses conditions d'utilisation.
