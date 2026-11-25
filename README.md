# Turbine Lens

A web application prototype for exploring energy-fleet data through a natural-language analytics interface.

## What it demonstrates

- A React client with a light and dark theme
- A FastAPI service
- Read-only SQL tool patterns for analytics
- Integration points for Databricks Genie and model services

## Repository map

- `client/` — web interface
- `server/` — API and service configuration
- `data/` and `resources/` — sample and deployment resources

## Run locally

See [GUIDE.md](GUIDE.md). The full application requires your own Databricks, Genie, model, and database services. No production conversation exports or business data are included.

## Scope

This is a prototype, not a production analytics service. Configure read-only permissions and review SQL controls before connecting a data source. See [VALIDATION.md](VALIDATION.md).