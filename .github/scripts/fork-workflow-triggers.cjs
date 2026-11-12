// Fork only: `node fork-workflow-triggers.cjs FILE` prints the events a workflow starts on (its
// top-level `on:`), one per line, so fork-follow.sh can refuse new upstream workflows outsiders can
// start. It reads only plain YAML: on any other shape it exits 2 with the reason on stderr, and the
// caller cannot clear the file. No dependencies. Tests: fork-workflow-triggers.test.cjs.
const fs = require("node:fs");

const EVENT = /^[a-z_]+$/;
const ON_KEY = /^(?:on|"on"|'on'):(?=[ \t]|$)(.*)$/;
const TOP_KEY = /^(?:[A-Za-z_][A-Za-z0-9_-]*|"on"|'on'):(?=[ \t]|$)/;
const ODD_BREAK = /\r(?!\n)|[\u0085\u2028\u2029]/;
// A line whose value is a block scalar (`run: |`, `- >-`, `key: &a |`): dashes, key, indicator.
const BLOCK_SCALAR = /^((?:-\s+)*)((?:[^\s#][^#]*?:\s+)?)(?:[&!]\S+\s+)*[|>][1-9+-]{0,2}$/;

const unquote = (s) => s.trim().replace(/^(["'])(.*)\1$/, "$2");
// Only ASCII spaces indent and only spaces and tabs are blank, as in YAML: a line that starts with
// a BOM or another Unicode space is unindented, so it must be a plain key.
const indentOf = (s) => s.match(/^ */)[0].length;
const blank = (s) => /^[ \t]*$/.test(s);

// One line, read the way YAML reads it, inside DEPTH open brackets: `code` is the line without its
// comment, `quote` says a quoted scalar is still open at its end, `depth` how many brackets are.
// A quote or bracket starts a scalar or list only where a token starts (`name: it's` is text).
const scan = (line, depth) => {
  let quote = null;
  let tokenStart = true;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quote === '"') {
      if (c === "\\") i++;
      else if (c === '"') [quote, tokenStart] = [null, false];
      continue;
    }
    if (quote === "'") {
      if (c === "'" && line[i + 1] === "'") i++;
      else if (c === "'") [quote, tokenStart] = [null, false];
      continue;
    }
    if (c === "#" && (i === 0 || /[ \t]/.test(line[i - 1]))) {
      return { code: line.slice(0, i).trimEnd(), quote: false, depth };
    }
    if (/[ \t]/.test(c)) continue;
    if (tokenStart && (c === '"' || c === "'")) quote = c;
    else if ((tokenStart || depth > 0) && (c === "[" || c === "{"))
      [depth, tokenStart] = [depth + 1, true];
    else if (depth > 0 && (c === "]" || c === "}")) [depth, tokenStart] = [depth - 1, false];
    else if (depth > 0 && (c === "," || c === ":")) tokenStart = true;
    else if ("-?:".includes(c) && (i + 1 === line.length || /[ \t]/.test(line[i + 1])))
      tokenStart = true;
    else tokenStart = false;
  }
  return { code: line.trimEnd(), quote: quote !== null, depth };
};

// The events TEXT (a workflow file) starts on, sorted. Throws when it cannot tell for sure.
const readTriggers = (text) => {
  const unsure = (why) => {
    throw new Error(`cannot read the triggers: ${why}`);
  };
  // YAML 1.1 also breaks lines at these, which a split on \n would not see.
  if (ODD_BREAK.test(text))
    unsure("a line break other than \\n or \\r\\n (a lone \\r, U+0085, U+2028 or U+2029)");
  const lines = text.split(/\r?\n/);
  if (lines.some((l) => /^\s*\t/.test(l))) unsure("a line is indented with a tab");

  // The code of each line (null: blank, a comment, a block scalar's text, a bracket list's later
  // lines). A quote left open at a line's end could carry a decoy `on:`, so it is refused. A list
  // may go over lines (upstream's `needs:` do) if those lines are indented more than its first; a
  // last line of only closing brackets may line up with it.
  const codes = [];
  let blockAbove = -1; // a block scalar's text is the lines indented more than this
  let depth = 0; // brackets open at the end of the last line
  let flowFrom = -1; // the line that opened them
  lines.forEach((line, i) => {
    if (blockAbove !== -1 && (blank(line) || indentOf(line) > blockAbove)) return codes.push(null);
    blockAbove = -1;
    const scanned = scan(line, depth);
    if (scanned.quote) unsure(`a quote is left open at the end of line ${i + 1}`);
    if (depth > 0) {
      const [here, from] = [indentOf(line), indentOf(lines[flowFrom])];
      const closing = /^[\]}]+$/.test(scanned.code.trim()) && scanned.depth === 0;
      if (!blank(scanned.code) && !(here > from || (closing && here === from))) {
        unsure(
          `line ${i + 1} is inside the bracket opened on line ${flowFrom + 1} but not indented more than that line`,
        );
      }
      depth = scanned.depth;
      return codes.push(null);
    }
    ({ depth } = scanned);
    if (depth > 0) flowFrom = i;
    const { code } = scanned;
    if (blank(code)) return codes.push(null);
    const body = code.trim();
    const block = body.match(BLOCK_SCALAR);
    if (block) {
      // Its text is indented more than its key, or than its `-` when it has no key.
      const [, dashes, key] = block;
      blockAbove = indentOf(code) + (key ? dashes.length : dashes ? dashes.lastIndexOf("-") : -1);
    }
    codes.push(code);
  });
  if (depth > 0) unsure(`the bracket opened on line ${flowFrom + 1} is never closed`);

  let found = -1;
  let seenContent = false;
  codes.forEach((code, i) => {
    if (code === null || indentOf(code) !== 0) return;
    if (code === "---" || code === "...") {
      if (seenContent) unsure("more than one YAML document");
      return;
    }
    seenContent = true;
    // This refuses `on :`, escaped or other quoted keys, `? on`, directives and a root list.
    if (!TOP_KEY.test(code)) unsure(`the unindented line \`${code}\` is not a plain \`key:\``);
    if (ON_KEY.test(code)) {
      if (found !== -1) unsure("`on:` appears twice");
      found = i;
    }
  });
  if (found === -1) unsure("no top-level `on:`");

  const events = [];
  const name = (raw) => {
    const event = unquote(raw);
    if (!EVENT.test(event)) unsure(`\`${raw.trim()}\` is not an event name`);
    events.push(event);
  };
  const rest = codes[found].match(ON_KEY)[1].trim();
  if (/^\[[^[\]{}]*\]$/.test(rest)) {
    rest
      .slice(1, -1)
      .split(",")
      .filter((s) => s.trim() !== "")
      .forEach(name);
  } else if (/^[[{&*|>!]/.test(rest)) {
    unsure(`\`on: ${rest}\` is a form this reader does not take`);
  } else if (rest !== "") {
    name(rest);
  } else {
    let childIndent = -1;
    for (const code of codes.slice(found + 1)) {
      if (code === null) continue;
      const indent = indentOf(code);
      if (indent === 0) break;
      if (childIndent === -1) childIndent = indent;
      if (indent < childIndent) unsure("the `on:` block is indented unevenly");
      if (indent > childIndent) continue;
      const body = code.trim();
      const item = body.match(/^-\s+(.*)$/);
      const key = body.match(/^("[^"]*"|'[^']*'|[^\s:"']+)\s*:(?=[ \t]|$)/);
      if (item) name(item[1]);
      else if (key && key[1] !== "<<") name(key[1]);
      else unsure(`\`${body}\` under \`on:\``);
    }
    if (childIndent === -1) unsure("`on:` is empty");
  }
  return [...new Set(events)].sort();
};

const main = () => {
  try {
    let text;
    try {
      text = fs.readFileSync(process.argv[2], "utf8");
    } catch (error) {
      throw new Error(
        `cannot read the triggers: cannot read ${process.argv[2]}: ${error.message}`,
        {
          cause: error,
        },
      );
    }
    const events = readTriggers(text);
    process.stdout.write(events.map((e) => `${e}\n`).join(""));
  } catch (error) {
    // Any failure, a bug included, means "cannot tell", which the caller refuses.
    process.stderr.write(`${error.message}\n`);
    process.exit(2);
  }
};

module.exports = { readTriggers };

if (require.main === module) main();
