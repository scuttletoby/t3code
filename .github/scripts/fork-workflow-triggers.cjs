// Fork only: prints the events a GitHub Actions workflow file starts on (the keys or names under
// its top-level `on:`), one per line, sorted. fork-follow.sh uses it to refuse an upstream release
// that adds a workflow someone outside the fork can start. No dependencies: the plan job installs
// nothing. Tests: fork-follow.test.sh.
//   node fork-workflow-triggers.cjs FILE
// Exit 2, with the reason on stderr, when it cannot tell for sure (no `on:`, two of them, tabs, a
// flow mapping, an anchor or alias, a block scalar, a list split over lines, a name that is not an
// event name). The caller then treats the file as one it cannot clear.
const fs = require("node:fs");

const unsure = (why) => {
  process.stderr.write(`cannot read the triggers: ${why}\n`);
  process.exit(2);
};

let text;
try {
  text = fs.readFileSync(process.argv[2], "utf8");
} catch (error) {
  unsure(`cannot read ${process.argv[2]}: ${error.message}`);
}

const uncomment = (s) => s.replace(/(^|\s)#.*$/, "").trimEnd();
const unquote = (s) => s.trim().replace(/^(["'])(.*)\1$/, "$2");
const indentOf = (s) => s.length - s.trimStart().length;
const EVENT = /^[a-z_]+$/;
const ON_KEY = /^(?:on|"on"|'on'):(?=\s|$)(.*)$/;

const lines = text.split(/\r?\n/);
if (lines.some((l) => /^\s*\t/.test(l))) unsure("a line is indented with a tab");

const events = [];
const name = (raw) => {
  const event = unquote(raw);
  if (!EVENT.test(event)) unsure(`\`${raw.trim()}\` is not an event name`);
  events.push(event);
};

let found = -1;
let seenContent = false;
lines.forEach((line, i) => {
  const code = uncomment(line);
  if (code.trim() === "") return;
  if (code === "---" || code === "...") {
    if (seenContent) unsure("more than one YAML document");
    return;
  }
  seenContent = true;
  if (indentOf(code) === 0 && ON_KEY.test(code)) {
    if (found !== -1) unsure("`on:` appears twice");
    found = i;
  }
});
if (found === -1) unsure("no top-level `on:`");

const rest = uncomment(lines[found]).match(ON_KEY)[1].trim();
if (rest.startsWith("[")) {
  if (!rest.endsWith("]")) unsure("the event list goes on over several lines");
  rest
    .slice(1, -1)
    .split(",")
    .filter((s) => s.trim() !== "")
    .forEach(name);
} else if (/^[{&*|>!]/.test(rest)) {
  unsure(`\`on: ${rest}\` is a form this reader does not take`);
} else if (rest !== "") {
  name(rest);
} else {
  let childIndent = -1;
  for (const line of lines.slice(found + 1)) {
    const code = uncomment(line);
    if (code.trim() === "") continue;
    const indent = indentOf(code);
    if (indent === 0) break;
    if (childIndent === -1) childIndent = indent;
    if (indent < childIndent) unsure("the `on:` block is indented unevenly");
    if (indent > childIndent) continue;
    const body = code.trim();
    const item = body.match(/^-\s+(.*)$/);
    const key = body.match(/^("[^"]*"|'[^']*'|[^\s:"']+)\s*:(?=\s|$)/);
    if (item) name(item[1]);
    else if (key && key[1] !== "<<") name(key[1]);
    else unsure(`\`${body}\` under \`on:\``);
  }
  if (childIndent === -1) unsure("`on:` is empty");
}

process.stdout.write([...new Set(events)].sort().map((e) => `${e}\n`).join(""));
