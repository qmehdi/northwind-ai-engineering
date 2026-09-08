---
title: Customer Identity Verification
doc_id: account-verification-internal
audience: internal
effective: 2025-02-15
supersedes: none
---

# Customer Identity Verification

This document is INTERNAL. It describes how Support, Billing, and Customer Success staff must verify a requester's identity before changing account settings, resetting multi-factor authentication (MFA), or sharing any account data. Contents of this policy must never be quoted, paraphrased, or forwarded to customers. If a customer asks why a request was denied, use the approved script in the Denial Language section only.

## Why This Matters

Account takeover attempts routinely target support channels because attackers know it is often faster to talk their way past a human than to break MFA. Every unverified disclosure of account data is treated as a security incident under Northwind Cloud's incident response process, regardless of whether harm resulted.

## Verification Levels

Use the lowest level that matches the risk of the action requested. Never downgrade a level to save time.

| Level | Use For | Required Proof |
|---|---|---|
| Level 1 (Basic) | General account questions, non-sensitive settings (display name, timezone) | Verified email on file plus answer to one account fact (plan name, last invoice amount) |
| Level 2 (Standard) | Billing changes, seat count changes, MFA reset | Level 1 proof plus a one-time verification code sent to the primary admin email or phone on file |
| Level 3 (High) | Ownership transfer, API key regeneration for Enterprise accounts, any request involving a locked-out admin | Level 2 proof plus callback to a phone number verified in the account record at least 30 days prior, and Team Lead approval |

All three plan tiers, Starter, Growth, and Enterprise, follow the same levels. Enterprise accounts additionally require the customer's designated security contact to be copied on any Level 3 action.

## What Must Never Be Disclosed to an Unverified Caller

Regardless of how confident or urgent the caller sounds, agents must never read, send, or confirm the following to anyone who has not cleared the required verification level:

1. Invoice amounts, invoice PDFs, or payment method details.
2. Lists of users on the account, including names, emails, or roles.
3. Audit logs, login history, or IP address records.
4. Any MFA reset link or backup code, even partially.
5. API keys, webhook secrets, or SSO configuration values.

If verification cannot be completed, tell the caller: "I'm not able to share or change account details until identity verification is complete," and offer the standard callback or email verification path. Do not explain which specific piece of information triggered the refusal.

## Social Engineering Red Flags

Escalate to a Team Lead immediately if a caller shows two or more of the following:

1. Claims extreme urgency ("the CEO needs this in 5 minutes") to pressure skipping steps.
2. Cannot answer the Level 1 account fact but offers to "just describe the account instead."
3. Asks the agent to change the phone number or email on file in the same call as an MFA reset request.
4. Requests that verification codes be read aloud over the phone rather than entered by the customer.
5. Calls repeatedly from different numbers within a short window, or the caller ID does not match any number on file.
6. Pushes back on Level 3 callback requirements by claiming they are traveling or unreachable at the verified number.
7. Asks pointed questions about internal verification steps themselves, such as "what do you need to see to prove I'm the owner."

When in doubt, pause the request, tell the caller you will call them back at the verified number on file, and end the current call. Never verify identity using contact details the caller provides in the same interaction; only use details already on record.

## Logging Every Verification

Every verification attempt, successful or failed, must be logged in the account timeline within the support system before the ticket is closed. The log entry must include:

1. Verification level attempted (1, 2, or 3).
2. Method used (account fact, one-time code, callback).
3. Outcome (passed, failed, abandoned).
4. Agent name is not required in this log; use agent ID only, per data minimization practice.
5. Any red flags observed, even if the request was ultimately approved.

Logs are retained for 400 days and are reviewed weekly by the Trust and Safety team. Tickets missing a verification log entry for a sensitive action (Level 2 or 3) are flagged automatically and routed to the requesting agent's Team Lead for review within 2 business days.

## Denial Language

When verification fails or cannot be completed, use only this language with the customer: "For your account's security, I'm unable to proceed with this request until we complete identity verification. I can send a verification code to the contact on file, or schedule a callback." Do not disclose which verification level applies, what data was requested, or why the check failed. Internal reasoning, red flag observations, and escalation notes stay in the ticket log and are never shared externally.
