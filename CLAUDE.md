# Massing Bill

See [`AGENTS.md`](AGENTS.md) for the operating guide (architecture, golden rules,
where things live, commands, CI expectations) and [`SPEC.md`](SPEC.md) for the
full specification and phase plan.

Three things that are easy to get wrong and expensive to fix:

1. **Money is integer cents.** Never a float, never `Numeric` on the money path.
2. **Standalone is the product.** Nothing in the core may import an optional
   adapter (`massing_cloud`, `oidc`, `s3`, `integrations`). CI deletes those
   modules and re-runs the suite.
3. **Never reproduce AIA form artwork or certification wording.** We model the
   line structure and the arithmetic only.
