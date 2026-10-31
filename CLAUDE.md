# Turbine Lens working rules

Keep SQL and agent tools read-only. Preserve per-session ownership checks, the SQL table allowlist and the validated-loss definition. Do not grant arbitrary shell or filesystem tools to the model.

The React UI is in `client/` and the FastAPI API is in `server/`. Components and palette are local. Run `npm run build` in `client/` after interface changes, and `python -m pytest tests/test_site_resolver.py -q` after matching changes.

Cloud integrations require user-supplied configuration. `databricks.yml` supplies a development example; it does not reproduce a live deployment. Keep credentials in environment variables or a secret manager.
