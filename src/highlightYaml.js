const LITERAL = /^(?:true|false|null|yes|no|on|off|~)$/i;
const NUMBER = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;

export function highlightYamlTokens(text) {
  return String(text ?? '').replace(/\r\n/g, '\n').split('\n').map(tokenizeYamlLine);
}

function token(kind, text) {
  return { kind, text };
}

function tokenizeYamlLine(line) {
  const tokens = [];
  let index = 0;
  while (index < line.length && line[index] === ' ') index += 1;
  if (index) tokens.push(token('', line.slice(0, index)));
  if (index >= line.length) return tokens;

  if (line[index] === '#') {
    tokens.push(token('comment', line.slice(index)));
    return tokens;
  }

  const trimmed = line.slice(index);
  if (trimmed === '---' || trimmed === '...') {
    tokens.push(token('punct', trimmed));
    return tokens;
  }

  if (line[index] === '-' && (index + 1 === line.length || line[index + 1] === ' ')) {
    tokens.push(token('punct', '-'));
    index += 1;
    if (line[index] === ' ') {
      tokens.push(token('', ' '));
      index += 1;
    }
  }

  if (index >= line.length) return tokens;

  const colon = findKeyColon(line, index);
  if (colon !== -1) {
    tokens.push(token('key', line.slice(index, colon)));
    tokens.push(token('punct', ':'));
    index = colon + 1;
    if (line[index] === ' ') {
      tokens.push(token('', ' '));
      index += 1;
    }
  }

  if (index < line.length) tokens.push(token(valueKind(line.slice(index)), line.slice(index)));
  return tokens;
}

function findKeyColon(line, start) {
  let cursor = start;
  if (line[cursor] === '"' || line[cursor] === "'") {
    cursor = endOfQuoted(line, cursor);
  } else {
    if (!/[A-Za-z0-9]/.test(line[cursor] || '')) return -1;
    cursor += 1;
    while (cursor < line.length && /[A-Za-z0-9_-]/.test(line[cursor])) cursor += 1;
  }
  return line[cursor] === ':' ? cursor : -1;
}

function endOfQuoted(line, start) {
  const quote = line[start];
  let cursor = start + 1;
  while (cursor < line.length) {
    if (line[cursor] === '\\') {
      cursor += 2;
      continue;
    }
    if (line[cursor] === quote) return cursor + 1;
    cursor += 1;
  }
  return line.length;
}

function valueKind(value) {
  if (value === '{}' || value === '[]') return 'punct';
  if (LITERAL.test(value)) return 'literal';
  if (NUMBER.test(value)) return 'number';
  return 'string';
}
