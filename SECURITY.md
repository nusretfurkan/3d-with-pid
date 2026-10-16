# Security

## Reporting

Report a suspected vulnerability privately through GitHub's private vulnerability reporting:

1. Open <https://github.com/nusretfurkan/3d-with-pid/security/advisories/new>, or go to the repository's **Security** tab and choose **Report a vulnerability**.
2. Describe the issue: the affected commit, your platform, steps to reproduce, and the impact you observed.
3. Leave out secrets, credentials and personal or client data. If a proof of concept needs a project file, use the synthetic example or a minimal file you are allowed to share.

Please do not open a public issue, pull request or discussion for a suspected vulnerability. There is no separate e-mail contact.

## Intended deployment

The software is designed for **one user on one machine**:

- The interactive service binds to `127.0.0.1` only. It accepts only its own `Host` and `Origin`, requires JSON content type and size limits on writes, sends no CORS headers, and serves files only from `web/` and its own build directories (with an extension allow-list and no traversal).
- No HTTP endpoint accepts a file path, program name or shell text. The build worker and the Claude Code CLI are started from argument lists the service builds itself, never through a shell.
- Generated SVG and HTML are checked for scripts, event handlers, external references and similar content before publication, and are served with a restrictive Content Security Policy and sandbox.
- Composed equipment definitions are data. Their expression language has no names beyond declared parameters, no attribute access and no calls beyond a fixed set of math functions, with bounded size and work.

Do not expose the service to a network, put it behind a shared reverse proxy, or run it for multiple users. Those set-ups are outside the security model and have not been assessed.

## External model calls

Engineering plan, broad Ask and equipment research are **opt-in**. They run the operator's own Claude Code CLI and send data to the operator's Claude Max subscription: the request plus a project snapshot for planning, or only the request text for research. The CLI runs with no tools except structured output (research: web search only), no MCP servers, no settings files and no session persistence. `ANTHROPIC_API_KEY` is removed from its environment. Replies are validated as untrusted input and cannot change a project without review and an explicit Apply. Only the authentication method and subscription type are read from `claude auth status`; account e-mail and organisation details are not stored or displayed. Decide whether your project data may be sent before you use these features.

## Scope

Reports about the local service, file handling, path traversal, generated-file sanitisation, the definition expression evaluator, subprocess handling and data sent to external models are in scope. The correctness of engineering outputs is a quality matter, not a security vulnerability; please use an issue for it.
