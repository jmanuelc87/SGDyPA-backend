# API v1 contract

`openapi.yaml` is the contract-first source of truth for the versioned `/api/v1/` surface.
Every PR that adds, removes, or changes an endpoint must update this contract in the same PR.

## Generate SPA TypeScript types

The generated client-facing types are versioned at `api/v1/generated/types.ts`.
Regenerate them after editing the OpenAPI contract:

```bash
npm run generate:api-types
```

The pipeline uses `openapi-typescript` and writes the generated artifact in place so the SPA can vendor or copy the committed types without needing backend runtime introspection.
