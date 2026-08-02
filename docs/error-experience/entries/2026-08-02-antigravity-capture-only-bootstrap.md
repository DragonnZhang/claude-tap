---
title: Antigravity capture-only failed before prompt generation
date: 2026-08-02
status: resolved
---

# Antigravity capture-only failed before prompt generation

## What broke

Antigravity CLI 1.1.6 and newer performs OAuth identity and Code Assist
eligibility checks before it sends a model request. Reverse-mode prompt export
redirected only `CLOUD_CODE_URL`, so the CLI still sent its user-info request to
Google with the synthetic capture token and exited with `401 Unauthorized`.

After the startup sequence was handled, the generation call used HTTP chunked
transfer encoding. The forward proxy only read requests with `Content-Length`,
so it recorded a `null` body and prompt export still failed.

## What did not fix it

Returning a completed `onboardUser` operation fixed the first setup error, but
the next identity check still bypassed the reverse proxy. Adding more fake token
fields also did not help because the CLI validates identity through an HTTP
request.

## Fix

Forward-mode capture now recognizes the Antigravity OAuth and Code Assist
bootstrap paths. In capture-only mode it returns deterministic local responses
for those non-model calls, while the existing generation path is still the only
request written to the trace. The forward proxy now also decodes chunked request
bodies before parsing and recording them.

## Lesson

Prompt export must model the complete request sequence required to reach the
model call. When a CLI adds startup network checks, capture-only support should
represent them at the protocol boundary instead of adding version-specific
retries or allowing synthetic credentials to reach a real identity service.
