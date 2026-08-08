"""Zero Develop web control surface.

Per ``zero-web-control-surface`` SKILL.md: the website is the primary
complete interface to the control plane. It presents projects, plans,
approvals, executions, agent types, tools, providers, budgets, and
evidence. It does not keep a parallel version of those facts in browser
state.

Per ``zero-interface-adapter-model`` SKILL.md: the website is a client
of backend contracts, not a second source of business truth. Its
requests pass through the same authorization and revision checks as bot
actions.

Per PLAN.md M12 invariants:
- Website is a client of backend contracts, not a second source of
  business truth.
- Permissions are enforced server-side.
- No dead buttons, fake metrics, mock execution results, or speculative
  management pages.
- Accessibility, responsive behavior, input validation, and error
  recovery are not optional.
- Sensitive data is minimized and never placed in client bundles or
  telemetry.
"""
