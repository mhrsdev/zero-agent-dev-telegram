# ADR 0023 — Web Control Surface as Backend Projection

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 12 (Primary Website Vertical Slices)
- Skills applied: `zero-web-control-surface`, `zero-interface-adapter-model`

## Context

`PLAN.md` §17 (Milestone 12) requires:
- Website is a client of backend contracts, not a second source of
  business truth.
- Permissions are enforced server-side.
- No dead buttons, fake metrics, mock execution results, or speculative
  management pages.
- Accessibility, responsive behavior, input validation, and error
  recovery are not optional.
- Sensitive data is minimized and never placed in client bundles or
  telemetry.

`zero-web-control-surface` §"The browser holds a projection": "Browser
state improves interaction but is not canonical. Every protected
mutation reaches a backend operation that revalidates actor, project,
revision, and transition."

`zero-interface-adapter-model` §"The website is primary, not privileged
around policy": "The website exposes the most complete management
experience, but its requests pass through the same authorization and
revision checks as bot actions."

## Decision

Adopt a server-rendered web control surface with:

1. **Server-side rendering**: HTML pages are rendered on the server
   using Jinja2 templates. The browser receives a complete HTML
   document; there is no client-side JavaScript framework or SPA. This
   is the smallest verifiable approach for Phase 7.

2. **Backend projection**: every page is a projection of durable
   backend state. The web controller calls the same application
   services (`IdentityService`, `PlanService`, `WorkerService`, etc.)
   that the JSON API uses. No parallel business logic exists in the web
   layer.

3. **Form-based mutations**: mutations use standard HTML forms with
   `POST` method. After a successful mutation, the browser is
   redirected to the relevant detail page (Post/Redirect/Get pattern),
   which displays the durable server state. This ensures that a page
   refresh shows canonical state, not a stale form submission.

4. **Authorization enforced server-side**: every mutation calls the
   application service, which calls `AuthorizationService.authorize`
   before performing the operation. The web layer does not implement
   its own authorization; it relies on the backend.

5. **Stale revision handling**: the plan approval form includes
   `expected_revision_number`. If the plan has been edited since the
   page was loaded, the backend returns a 409 Conflict, which the web
   layer surfaces as an error.

6. **Accessible semantics**: all pages include:
   - A skip link for screen readers.
   - Semantic HTML landmarks (`<nav>`, `<main>`).
   - `<label>` elements for all form inputs.
   - `aria-label` on navigation.
   - `role="alert"` and `role="status"` on alert messages.
   - Keyboard-focusable elements with `:focus-visible` styling.
   - A viewport meta tag for mobile.

7. **Responsive design**: a CSS stylesheet with a mobile breakpoint at
   768px. Grid layouts collapse to single column on narrow screens.
   Font sizes and spacing are reduced on mobile.

8. **No secrets in HTML**: the website never displays secret values.
   Secret management pages show only metadata (name, type, created_at,
   revoked_at). The test `test_no_secrets_in_html` verifies that a
   known secret value does not appear on any page.

9. **Empty states**: pages display "No X yet" messages when there are
   no records, rather than empty tables or error states.

10. **Error states**: errors are surfaced as typed HTTP status codes
    (400 for bad input, 403 for forbidden, 404 for not found, 409 for
    conflict) with human-readable error messages.

## Rejected alternatives

- **Client-side SPA (React/Vue)**: rejected for Phase 7. Adds a build
  step, a JavaScript runtime, and client-side state management before
  the backend has stabilized. Server-side rendering is simpler and
  exercises the same backend APIs.
- **Dead buttons / mocked data**: explicitly rejected by PLAN.md M12:
  "No dead buttons, fake metrics, mock execution results, or
  speculative management pages." Every UI action performs a real
  backend operation.
- **Client-side authorization**: explicitly rejected by
  `zero-web-control-surface` §"Menu hiding as authorization" and
  `zero-control-plane-trust` §"UI controls are not security".
- **Secrets returned to client**: explicitly rejected by
  `zero-web-control-surface` §"Secret and sensitive views minimize
  exposure". Masked text is still delivered secret material.

## Consequences

- Each published UI action performs a real authorized backend operation
  and displays durable server state after refresh.
- A surface with no verified backend remains absent, not mocked.
- The website is accessible (skip links, labels, landmarks, keyboard
  navigation, focus management).
- The website is responsive (mobile viewport, collapsing grids).
- No secrets appear in HTML, client state, or network responses.
- Stale revisions are detected and surfaced as 409 Conflict.
- The web layer can be rolled back independently of the backend (a
  frontend rollback does not corrupt backend state).
