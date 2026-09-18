// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// Robust decoder for the AgentCore runtime's streamed body. Mirrors aiq/pipe/aiq_agentcore_pipe.py `_invoke`:
//  * newline-delimited JSON, optionally SSE-framed (`data: {...}`, `: comment`, `event:`/`id:` lines)
//  * BedrockAgentCoreApp wraps yielded strings as JSON strings, so a line may be `"{\"type\":...}"` (double-encoded)
//    or even an unquoted escaped string; both are unwrapped
//  * chunk boundaries may split a line: NdjsonDecoder buffers partial lines across push() calls

export type DecodedEvent = Record<string, unknown>;

function tryParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return undefined;
  }
}

/** Decode one line; returns undefined for blanks, comments, SSE control lines and undecodable content. */
export function decodeLine(raw: string): DecodedEvent | undefined {
  let s = raw.trim();
  if (!s || s.startsWith(':')) return undefined;
  if (/^(event|id|retry):/i.test(s)) return undefined;
  if (s.startsWith('data:')) s = s.slice(5).trim();
  if (!s) return undefined;
  let obj = tryParse(s);
  if (obj === undefined) {
    // an unquoted, escaped JSON string: quote it, decode the string, then parse the payload
    const inner = tryParse(`"${s}"`);
    obj = typeof inner === 'string' ? tryParse(inner) : undefined;
  }
  for (let depth = 0; typeof obj === 'string' && depth < 3; depth++) obj = tryParse(obj);
  if (obj && typeof obj === 'object' && !Array.isArray(obj)) return obj as DecodedEvent;
  return undefined;
}

export class NdjsonDecoder {
  private buf = '';

  push(chunk: string): DecodedEvent[] {
    this.buf += chunk;
    const out: DecodedEvent[] = [];
    let i: number;
    while ((i = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, i);
      this.buf = this.buf.slice(i + 1);
      const ev = decodeLine(line);
      if (ev) out.push(ev);
    }
    return out;
  }

  /** Call once at end-of-stream: the last line may lack a trailing newline. */
  flush(): DecodedEvent[] {
    const rest = this.buf;
    this.buf = '';
    const ev = decodeLine(rest);
    return ev ? [ev] : [];
  }
}

export function decodeAll(text: string): DecodedEvent[] {
  const d = new NdjsonDecoder();
  return [...d.push(text), ...d.flush()];
}
