# Git Synapse Helm chart

The chart installs the API, scheduler, MCP server, a bundled PostgreSQL
instance, database migration hook, health probes, persistent mirrors, and an
optional Ingress.

The default application image is the public Docker Hub image
`docker.io/kirankn8/git-synapse`. Kubernetes does not need an image-pull Secret
unless the image is made private.

## Install

```bash
helm upgrade --install git-synapse \
  oci://ghcr.io/kirankn8/charts/git-synapse \
  --namespace git-synapse \
  --create-namespace
```

The default chart generates a random first-admin setup token and stores it in
the Kubernetes Secret `git-synapse`. Retrieve it with:

```bash
kubectl -n git-synapse get secret git-synapse \
  -o jsonpath='{.data.ADMIN_SETUP_TOKEN}' | base64 -d; echo
```

The token is valid only until the first administrator is created. The chart
prints this command again in Helm's post-install notes. The Secret remains in
Kubernetes, but the application refuses setup once any account exists.

By default the API is internal. Use the temporary port-forward shown by
`helm get notes git-synapse -n git-synapse`, or enable an Ingress:

```bash
helm upgrade git-synapse oci://ghcr.io/kirankn8/charts/git-synapse \
  -n git-synapse --reuse-values \
  --set ingress.enabled=true \
  --set ingress.hosts[0].host=synapse.example.com
```

The bundled Postgres and mirror PVC are convenient defaults. For production,
set `postgresql.enabled=false`, provide `externalDatabase.host`, and use a
managed Postgres plus an RWX storage class for the shared mirror PVC.

## Upgrade and rollback

```bash
helm repo update  # if using a repository mirror
helm upgrade git-synapse oci://ghcr.io/kirankn8/charts/git-synapse \
  -n git-synapse --reuse-values --version 1.0.1
helm rollback git-synapse -n git-synapse
```
