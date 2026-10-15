/* Versioned server-default adoption. Existing configurations are never erased. */
(function (root) {
  function applyDefault(registry, selectedId, server, appliedRevision) {
    const models = Array.isArray(registry) ? registry.filter((m) => m && typeof m.id === 'string') : [];
    if (!server?.id || !server.path || server.provider !== 'local') {
      return { models, selectedId, revision: appliedRevision };
    }
    const normalize = (path) =>
      String(path || '')
        .replaceAll('\\', '/')
        .toLowerCase();
    const existing =
      models.find(
        (m) => m.id === server.id && m.provider === 'local' && normalize(m.path) === normalize(server.path),
      ) || models.find((m) => m.provider === 'local' && normalize(m.path) === normalize(server.path));
    const current = existing || { id: server.id, name: server.name, provider: 'local', path: server.path };
    const upgraded = appliedRevision !== server.revision;
    // First adoption selects the user-requested new default. Later page loads
    // preserve a deliberate switch back to another saved model.
    const ordered = upgraded
      ? [current, ...models.filter((m) => m.id !== current.id)]
      : existing
        ? models
        : [...models, current];
    return {
      models: ordered,
      selectedId: upgraded || !ordered.some((m) => m.id === selectedId) ? current.id : selectedId,
      revision: server.revision,
    };
  }
  root.ModelDefaults = { applyDefault };
})(typeof globalThis === 'object' ? globalThis : window);
