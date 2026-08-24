"use strict";

const fs = require("node:fs");

const originalStringify = JSON.stringify;
const outputPath = process.env.CLAUDE_TAP_QODER_PROMPT_PATH;

function looksLikePromptRequest(value) {
  return (
    value &&
    typeof value === "object" &&
    Array.isArray(value.messages) &&
    (typeof value.system === "string" || Array.isArray(value.tools))
  );
}

function safeModelConfig(value) {
  if (!value || typeof value !== "object") {
    return {};
  }
  const out = {};
  for (const key of ["key", "display_name", "model", "format", "source"]) {
    if (["string", "number", "boolean"].includes(typeof value[key])) {
      out[key] = value[key];
    }
  }
  return out;
}

JSON.stringify = function claudeTapQoderStringify(value, ...args) {
  const serialized = originalStringify(value, ...args);
  if (outputPath && !globalThis.__claudeTapQoderPromptWritten && looksLikePromptRequest(value)) {
    globalThis.__claudeTapQoderPromptWritten = true;
    try {
      const snapshot = {
        model_config: safeModelConfig(value.model_config),
        system: value.system,
        messages: value.messages,
        tools: Array.isArray(value.tools) ? value.tools : [],
      };
      fs.writeFileSync(outputPath, originalStringify(snapshot), {
        encoding: "utf8",
        mode: 0o600,
      });
    } catch {
      globalThis.__claudeTapQoderPromptWritten = false;
    }
  }
  return serialized;
};
