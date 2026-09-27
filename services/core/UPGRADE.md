# Upgrading an existing installation

This applies once: to a checkout that already has canonical state — work
items, artifacts, deliveries — recorded from before this service's
directory, compose project and containers were renamed (the rename that
produced this directory; see `docker-compose.yml`'s own header for the
container names). A fresh install has nothing to carry across and does not
need this page.

## Why this is not optional

Compose derives a named volume's identity from the compose project name,
and the project name comes from the directory `docker compose` is run in.
Renaming the directory alone moves both of this service's volumes to
identities that have never existed — Compose creates them empty, and every
canonical work item, artifact and delivery record already on disk is
silently stranded. Pulling a version with the rename and starting it
without the copy below looks like a clean start. It is actually the loss
of everything recorded so far.

Separately: this service's Redis consumer groups also renamed, from the
old service name to `core`. A group is state Redis keeps for the consumers
reading a stream, and the old group is not renamed or migrated — it is
abandoned. Any entry a consumer has not yet acknowledged in the old group
stays pending there forever once nothing reads it again. That is why the
stack must be brought down cleanly — every consumer stopped, nothing left
mid-delivery — before you upgrade, not stopped abruptly or left running
through the pull.

## Order

1. **Stop the stack cleanly.** Bring down Redis, this service and
   ScrumMaster (and any project containers), so nothing is mid-delivery
   when you pull.
2. **Pull and rename.** Pull the version that renames this service's
   directory, compose project and containers.
3. **Copy the two named volumes**, once, before the first start under the
   new name:

   ```bash
   docker run --rm -v work-item-service_workitem-postgres-data:/from \
     -v core_postgres-data:/to alpine sh -c 'cp -a /from/. /to/'
   docker run --rm -v work-item-service_artifact-data:/from \
     -v core_artifact-data:/to alpine sh -c 'cp -a /from/. /to/'
   ```

   This is a filesystem copy of two volumes, not a database migration —
   nothing about the schema or the data it holds changes.
4. **Start the stack back up** under the new name.

## The old volumes stay

The copy leaves the two original volumes exactly as they were. Nothing
here deletes them, so the step is reversible until you decide to prune
them yourself — `docker volume rm <name>`, once you have confirmed the
new volumes hold what you expect.
