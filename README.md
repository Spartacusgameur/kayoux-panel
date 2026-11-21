# Kayoux Panel

Panel d'administration privé pour le bot Discord Kayoux.

## Fichiers
- `app.py` : application web Flask et API privée pour le bot.
- `requirements.txt` : dépendances Python.
- `render.yaml` : configuration de déploiement Render.

## Variables d'environnement Render
- `FLASK_SECRET_KEY` : longue chaîne aléatoire secrète.
- `DISCORD_CLIENT_ID` : identifiant de l'application Discord.
- `DISCORD_CLIENT_SECRET` : secret OAuth2 Discord.
- `DISCORD_REDIRECT_URI` : `https://VOTRE-SERVICE.onrender.com/oauth/callback`
- `DISCORD_GUILD_ID` : identifiant du serveur Discord Kayoux.
- `ADMIN_ROLE_ID` : identifiant du rôle adminkayoux.
- `BOT_API_TOKEN` : longue clé secrète partagée avec le bot.

Dans Discord Developer Portal > OAuth2, ajoute exactement l'URL de redirection Render.
Scopes OAuth demandés : `identify` et `guilds.members.read`.

Ne place jamais les secrets dans GitHub. Configure-les dans Render > Environment.
Le panel et le bot doivent ensuite être reliés avec les endpoints `/api/transfers/sync`
et `/api/bot/commands`.
