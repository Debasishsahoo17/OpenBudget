# Security and privacy notes

OpenBudget is a **working civic-tech reference demo**, not a certified election, official municipality platform, or public procurement system. Do not use it for legally binding ballots or real-world spending authorizations without significant additional safeguards.

## Implemented

- Parameterized SQLite statements with foreign keys and unique ballot constraints.
- Atomic vote replacement, state transitions and allocation transactions (`BEGIN IMMEDIATE`).
- Passwords stored as PBKDF2-HMAC-SHA256 with a unique random salt and 300,000 iterations.
- Random server-side session tokens; only SHA-256 digests are stored in SQLite.
- CSRF tokens on all state-changing API requests.
- HttpOnly, SameSite=Lax cookies. Opt-in `OPENBUDGET_SECURE_COOKIES=1` in HTTPS deployments.
- Allowlisted static files, JSON-only mutations, request-size limits, security headers and a restrictive Content Security Policy.
- Role checks performed by the backend (moderator permissions are never delegated to the UI).
- HTML escaping for user-generated content.
- Downloadable data contains counts, never individual ballot records or private account contact details.
- CSV-export spreadsheet-formula mitigation.
- Private moderation records and submitted/pending proposals excluded from public endpoints.

## Before public deployment

1. **Verify legal eligibility and residency** with an appropriate, privacy-preserving identity assurance process. A new email account can currently vote; this is **not** one-human-one-vote.
2. Add rate limiting, bot protection, email verification and automated abuse detection at a reverse proxy and application level.
3. Deploy HTTPS end-to-end (or a trusted TLS-terminating proxy) with Secure cookies, HSTS, encrypted backups and access controls.
4. Add password reset, account recovery, session revocation UI, administrator provisioning and an operational access review. Never use demo credentials on the internet.
5. Implement independent tamper-evident ballot and audit verification if legally required; moderators with database access can modify these records in this demonstration.
6. Introduce independent fairness/legal review for budgeting rules and tie-breaks; the current greedy algorithm is a clearly documented **example**, not an optimal or legally mandated model.
7. Add accessibility audits, penetration tests, server-side logging/monitoring and a full data privacy impact assessment.
8. Upgrade data storage and multi-instance deployment architecture for real-world load (PostgreSQL, managed sessions, job queue, disaster recovery).
9. Publish actual spending milestones separately. "Funded" in the demo means **budget allocated**, not that the project has been delivered.
10. Review the supplied `Dockerfile` and secret management for your actual hosting environment.

## Disclosing vulnerabilities

Please report suspected vulnerabilities privately to the repository owner rather than posting exploit details in public issues. This reference repository does not designate a monitored security inbox.
