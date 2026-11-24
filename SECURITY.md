# Security policy

## Reporting a vulnerability

Please report security problems through GitHub's private vulnerability
reporting: open the **Security** tab of this repository and choose
**Report a vulnerability**. That opens a private advisory only the maintainers
can see.

Please do not open a public issue, and do not include API tokens, customer data
or anything else sensitive in a report. If a token may have been exposed,
revoke it in the console (**Settings → API Tokens**) before you write.

## What to include

- the version of `thunderduck-sqlalchemy`, your Python version and platform;
- what the problem allows an attacker to do, and under what conditions;
- the smallest reproduction you can manage, with secrets removed.

## What to expect

We read every report. We will confirm the report, tell you what we intend to
do, and coordinate a disclosure date with you. This is a small team, so we
cannot promise a fixed turnaround, and we do not run a paid bounty programme.
Credit is given in the advisory unless you ask otherwise.

## Scope

This repository contains a client library. The service it talks to is operated
by Thunderduck; vulnerabilities in the service are also in scope for a report
here, and will be handled the same way.
