/* Presentation only. Stable card IDs, request keys and inbox routes are not renamed. */
(() => {
  const legacy = new Set(['diagnostic-agent', 'gateway-agent']);
  function cardName(card = {}) {
    const operation = card.operation || card.card;
    if (operation !== 'agent.resident' && !legacy.has(card.id)) return null;
    return (card.params || card.parameters || {}).role === 'operator' ? 'Operator agent' : 'Gateway agent';
  }
  function render(container, agent, card) {
    if (!container._gatewayParts) {
      const summary = document.createElement('p'), details = document.createElement('details');
      const heading = document.createElement('summary'), raw = document.createElement('pre');
      heading.textContent = 'Technical connection details (IDs and raw records)';
      details.append(heading, raw); container.replaceChildren(summary, details);
      container._gatewayParts = {summary, raw};
    }
    const session = agent?.session || {}, model = agent?.model_info || {};
    const requests = agent?.requests || [];
    const pending = requests.filter(r => ['queued', 'running'].includes(r.status)).length;
    container._gatewayParts.summary.textContent = [cardName(card) || 'Gateway agent',
      agent?.quarantined ? 'Review required' : (session.status || (agent?.connected ? 'Connected' : 'Disconnected')), session.provider,
      model.resolved_model, model.resolved_effort, `${pending} pending requests`].filter(Boolean).join(' · ');
    // Keep the original evidence available explicitly, without replacing IDs in it.
    container._gatewayParts.raw.textContent = JSON.stringify(agent || {status: 'disconnected'}, null, 2);
  }
  window.ldconGatewayPresentation = {cardName, render};
})();
