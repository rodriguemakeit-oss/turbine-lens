# Turbine Lens — développement

## Interface

```bash
cd client
npm ci
npm run dev
# vérifier et compiler
npm run build
```

Le serveur Vite utilise le port 3000 et transmet `/api` à `http://127.0.0.1:8000`.

Les composants de `client/src/components/ui.tsx`, la palette vert profond/ambre et le symbole SVG sont locaux. Le thème clair/sombre est conservé dans le navigateur.

## API et intégrations

```bash
python -m venv .venv
# activez cet environnement
pip install -r requirements.txt
python -m uvicorn server.app:app --host 127.0.0.1 --port 8000
```

L’API utilise vos services Databricks, Genie, MLflow et PostgreSQL/Lakebase. Configurez les variables d’environnement listées dans `.env.example` et dans `server/config.py`. Le lancement complet nécessite ces services ; une API simulée n’est pas fournie.

Les exports de Genie, exemples issus de conversations et chiffres commerciaux ne sont pas inclus. Les espaces Genie se configurent avec vos propres identifiants. Les tarifs restent inconnus jusqu’à votre configuration.

## Tests sans cloud

```bash
pip install pytest
python -m pytest tests/test_site_resolver.py -q
```

Le jeu de noms de sites est fictif. Les tests couvrent les accents, les apostrophes et les correspondances ambiguës.
