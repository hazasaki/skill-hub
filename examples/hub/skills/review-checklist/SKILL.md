---
name: review-checklist
description: Use when reviewing a small code change and reporting findings with observable evidence.
category: software-engineering
tags: review, code, checklist, testing
risk: safe
license: MIT
---

# Review a small change

Read the change and its callers. Identify the requested behavior, the inputs that
could break it, and any handling that protects user data.

Use [the checklist](references/checklist.md) to organize the review. Report only
actionable findings supported by code or a repeatable check. State what was tested
and which important behavior remains unverified.

This example provides instruction data. Loading it does not execute a check or
grant permission to change files or contact external services.
