# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Voice Claude Agent, please:

1. **Do NOT open a public GitHub issue.** Instead, report it privately to the
   repository owner via GitHub's security advisory feature or email.
2. Provide a clear description of the vulnerability, including:
   - Steps to reproduce
   - Affected version(s)
   - Potential impact
3. Allow reasonable time for the maintainer to respond and address the issue
   before disclosing it publicly.

## Supported Versions

| Version | Supported |
|---------|-----------|
| v0.1.0  | Yes       |

## What We Consider a Vulnerability

- Unauthorized access to microphone data
- Local log files containing sensitive information beyond what is documented
- Remote code execution vectors through the Claude CLI interface
- Bypass of the high-risk action confirmation flow

## What Is Not a Vulnerability

- The fact that transcripts are sent to Claude CLI (this is documented behavior;
  Claude CLI's own privacy policy governs how it handles prompts)
- macOS TCC permission behavior (this is an OS-level concern)
- Standard Python dependency vulnerabilities (update dependencies regularly)

## No Telemetry, No Data Collection

Voice Claude Agent does not collect telemetry, analytics, or usage data. All
audio processing and logging happens locally on your machine. There is no
"phone home" or remote logging.

## Dependencies

We pin dependencies via `pyproject.toml`. If a dependency has a known CVE,
please report it so we can update the minimum version requirement.
